# Proxmox host (`pve`) monitoring

Host-side config for the hypervisor at `192.168.5.1`. This is **not** cluster
config — these files are installed on the Proxmox host itself, and scraped by the
in-cluster Prometheus via the `proxmox-host` job in
`../prometheus/additional-scrape-configs.yml`.

## Why a textfile collector exists

`node_exporter`'s built-in zfs collector reads `/proc/spl/kstat/zfs`, which exposes
ARC, I/O, and per-dataset statistics — **but not pool health**. On this host it emits
440 `node_zfs_*` metrics and not one of them reports `DEGRADED`.

That gap is not theoretical. On 2026-09-12 `main-pool` was found running degraded on a
single disk, and had been for weeks. `node_exporter` alone would not have caught it.

`zfs-textfile-collector.sh` fills the gap by shelling out to `zpool` and writing
metrics for the node_exporter textfile collector:

| Metric | Notes |
|---|---|
| `zpool_health{pool}` | 0=ONLINE 1=DEGRADED 2=FAULTED 3=OFFLINE/REMOVED/UNAVAIL 4=UNKNOWN |
| `zpool_capacity_percent{pool}` | |
| `zpool_fragmentation_percent{pool}` | |
| `zpool_device_errors{pool,device,type}` | `read` / `write` / `cksum` |
| `zpool_scrub_age_seconds{pool}` | absent if the pool has never been scrubbed |
| `zpool_scrub_in_progress{pool}` | |
| `zpool_vdevs_total{pool}` | |
| `zpool_collector_last_run_timestamp_seconds` | staleness canary — alerts if the collector stops |

It writes to a temp file and `mv`s it into place so node_exporter never reads a
partially written file.

SMART and NVMe metrics are **not** produced here — the Debian
`prometheus-node-exporter-collectors` package already ships `smartmon` and `nvme`
timers that cover those.

## Install

```sh
apt-get install -y prometheus-node-exporter          # pulls in -collectors too

install -m 755 zfs-textfile-collector.sh /usr/local/bin/zfs-textfile-collector.sh
install -m 644 prometheus-zfs.service /etc/systemd/system/
install -m 644 prometheus-zfs.timer   /etc/systemd/system/

systemctl daemon-reload
systemctl enable --now prometheus-zfs.timer
systemctl start prometheus-zfs.service               # run once immediately
```

The Debian package patches `--collector.textfile.directory` to
`/var/lib/prometheus/node-exporter` by default, so no `ARGS` change is needed in
`/etc/default/prometheus-node-exporter`.

## Verify

```sh
curl -s localhost:9100/metrics | grep '^zpool_health'
curl -s localhost:9100/metrics | grep '^node_textfile_scrape_error'   # must be 0
```

To prove detection actually works (rather than just that the metric exists), build a
throwaway file-backed mirror and degrade it. `cachefile=none` keeps it out of
`/etc/zfs/zpool.cache` so it can never be imported at boot:

```sh
mkdir -p /var/tmp/zfstest && truncate -s 200M /var/tmp/zfstest/{a,b}
zpool create -o cachefile=none alerttest-pool mirror /var/tmp/zfstest/a /var/tmp/zfstest/b
zpool offline alerttest-pool /var/tmp/zfstest/b
/usr/local/bin/zfs-textfile-collector.sh
grep alerttest-pool /var/lib/prometheus/node-exporter/zfs.prom   # expect zpool_health ... 1

zpool destroy alerttest-pool && rm -rf /var/tmp/zfstest
```

## Alerts

Rules live in `../prometheus/proxmox-host-alerts.yml` (`PrometheusRule`,
namespace `monitoring`).

**Known limitation:** Prometheus runs in VMs hosted by this machine. It will catch a
degraded pool, capacity, SMART wear, and scrub staleness — but it **cannot** alert on
total host failure, because it dies with the host. Covering that requires a dead-man's
switch: route the always-firing `Watchdog` alert to an external heartbeat service so
silence itself becomes the alarm.

## Related

- `../docs/superpowers/specs/2026-09-12-proxmox-bulk-storage-design.md`
