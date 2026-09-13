#!/usr/bin/env bash
# ZFS pool state metrics for prometheus node_exporter textfile collector.
#
# The built-in node_exporter zfs collector reads /proc/spl/kstat/zfs, which
# exposes ARC/IO/dataset statistics but NOT pool health. A degraded pool is
# therefore invisible to it. This script fills that gap.
#
# Installed at: /usr/local/bin/zfs-textfile-collector.sh
# Run by:       prometheus-zfs.timer (every 5 min)
# Writes:       /var/lib/prometheus/node-exporter/zfs.prom

set -uo pipefail

OUT_DIR="/var/lib/prometheus/node-exporter"
OUT="${OUT_DIR}/zfs.prom"
TMP="$(mktemp "${OUT}.XXXXXX")"
trap 'rm -f "$TMP"' EXIT

health_code() {
  case "$1" in
    ONLINE)              echo 0 ;;
    DEGRADED)            echo 1 ;;
    FAULTED)             echo 2 ;;
    OFFLINE|REMOVED|UNAVAIL) echo 3 ;;
    *)                   echo 4 ;;
  esac
}

pools="$(zpool list -H -o name 2>/dev/null)"

{
  echo "# HELP zpool_health Pool health (0=ONLINE 1=DEGRADED 2=FAULTED 3=OFFLINE/REMOVED/UNAVAIL 4=UNKNOWN)"
  echo "# TYPE zpool_health gauge"
  zpool list -H -o name,health 2>/dev/null | while read -r name health; do
    [ -n "${name:-}" ] || continue
    printf 'zpool_health{pool="%s"} %s\n' "$name" "$(health_code "$health")"
  done

  echo "# HELP zpool_capacity_percent Percent of pool capacity allocated"
  echo "# TYPE zpool_capacity_percent gauge"
  zpool list -H -o name,capacity 2>/dev/null | while read -r name cap; do
    [ -n "${name:-}" ] || continue
    printf 'zpool_capacity_percent{pool="%s"} %s\n' "$name" "${cap%\%}"
  done

  echo "# HELP zpool_fragmentation_percent Pool fragmentation percent"
  echo "# TYPE zpool_fragmentation_percent gauge"
  zpool list -H -o name,fragmentation 2>/dev/null | while read -r name frag; do
    [ -n "${name:-}" ] || continue
    f="${frag%\%}"
    case "$f" in ''|*[!0-9]*) f=0 ;; esac
    printf 'zpool_fragmentation_percent{pool="%s"} %s\n' "$name" "$f"
  done

  echo "# HELP zpool_device_errors Per-vdev cumulative error counters"
  echo "# TYPE zpool_device_errors gauge"
  for p in $pools; do
    zpool status "$p" 2>/dev/null | awk -v pool="$p" '
      NF==5 && $2 ~ /^(ONLINE|DEGRADED|FAULTED|OFFLINE|REMOVED|UNAVAIL)$/ {
        dev=$1; gsub(/[\\"]/,"",dev)
        r=$3; w=$4; c=$5
        # skip non-numeric (e.g. header rows)
        if (r ~ /^[0-9]+$/ && w ~ /^[0-9]+$/ && c ~ /^[0-9]+$/) {
          printf "zpool_device_errors{pool=\"%s\",device=\"%s\",type=\"read\"} %s\n",  pool, dev, r
          printf "zpool_device_errors{pool=\"%s\",device=\"%s\",type=\"write\"} %s\n", pool, dev, w
          printf "zpool_device_errors{pool=\"%s\",device=\"%s\",type=\"cksum\"} %s\n", pool, dev, c
        }
      }'
  done

  echo "# HELP zpool_scrub_age_seconds Seconds since last completed scrub or resilver"
  echo "# TYPE zpool_scrub_age_seconds gauge"
  now="$(date +%s)"
  for p in $pools; do
    line="$(zpool status "$p" 2>/dev/null | grep -E '(scrub repaired|resilvered) .* on ' | head -1)"
    [ -n "$line" ] || continue
    d="${line##* on }"
    ts="$(date -d "$d" +%s 2>/dev/null || true)"
    [ -n "$ts" ] && printf 'zpool_scrub_age_seconds{pool="%s"} %s\n' "$p" "$((now - ts))"
  done

  echo "# HELP zpool_scrub_in_progress 1 if a scrub or resilver is currently running"
  echo "# TYPE zpool_scrub_in_progress gauge"
  for p in $pools; do
    if zpool status "$p" 2>/dev/null | grep -qE 'scan:.*(in progress)'; then
      printf 'zpool_scrub_in_progress{pool="%s"} 1\n' "$p"
    else
      printf 'zpool_scrub_in_progress{pool="%s"} 0\n' "$p"
    fi
  done

  echo "# HELP zpool_vdevs_total Number of leaf/interior vdevs reported for the pool"
  echo "# TYPE zpool_vdevs_total gauge"
  for p in $pools; do
    n="$(zpool status "$p" 2>/dev/null | awk 'NF==5 && $2 ~ /^(ONLINE|DEGRADED|FAULTED|OFFLINE|REMOVED|UNAVAIL)$/' | wc -l)"
    printf 'zpool_vdevs_total{pool="%s"} %s\n' "$p" "$n"
  done

  # Staleness canary: lets Prometheus alert if this collector stops running.
  echo "# HELP zpool_collector_last_run_timestamp_seconds Unix time of last successful collector run"
  echo "# TYPE zpool_collector_last_run_timestamp_seconds gauge"
  printf 'zpool_collector_last_run_timestamp_seconds %s\n' "$(date +%s)"
} > "$TMP"

chmod 644 "$TMP"
mv -f "$TMP" "$OUT"   # atomic swap so node_exporter never reads a partial file
trap - EXIT
