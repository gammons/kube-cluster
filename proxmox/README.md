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

## Cluster snapshots

`snapshot-cluster.sh` snapshots all 4 k3s VMs plus `main-pool/k3s-nfs` as one
unit — together they hold the k3s SQLite datastore, all 23 `local-path` PVCs and
all 15 `nfs` PVCs. Velero is not a substitute; it covers a subset of namespaces
and no VM-level state.

```sh
./snapshot-cluster.sh create   pre-<change>   # gate before any upgrade
./snapshot-cluster.sh list
./snapshot-cluster.sh rollback pre-<change>   # stops, reverts and restarts all 4 VMs
./snapshot-cluster.sh delete   pre-<change>   # once the change is confirmed good
```

**Labels accept `[A-Za-z0-9_-]` only, and may not start with `-`.** Dots are
rejected, so `pre-k3s-v1.30.5` fails — use `pre-k3s-v1-30-5`.

`create` refuses to run unless every ZFS pool is healthy and `qemu-guest-agent`
answers on all 4 VMs, so a snapshot is never silently crash-consistent. It also
refuses a label already in use. `rollback` verifies the label exists on all five
targets and that all 4 VMs actually stopped before it reverts anything, so a
partial rollback cannot leave the cluster split across two points in time.

### Only the most recent set can be rolled back to

**Hold at most one gate at a time.** The VM disks are on `zfspool` storage, and
Proxmox refuses to roll a zvol back to anything but its newest snapshot:

```
can't rollback, '<snap>' is not most recent snapshot on '<volid>'
```

(`PVE/Storage/ZFSPoolPlugin.pm`, `volume_rollback_is_possible`.)

The two halves of a set do not fail the same way, which is why this needs saying
out loud rather than being left to the tools:

| Half | With a newer snapshot present |
|---|---|
| 4 VMs (`qm rollback`) | **refuses** — dies on the first VM |
| `main-pool/k3s-nfs` (`zfs rollback -r`) | **succeeds, destroying** every newer dataset snapshot |

So `rollback` checks **recency as well as presence** on all five targets, before
the confirmation prompt and before any VM is stopped. If a newer set exists it
names the blocking labels and stops, having changed nothing. Without that check
the old behaviour was: pre-flight passes, all 4 VMs stop, the first `qm rollback`
dies — cluster powered off, nothing reverted, mid-incident.

To reach an older set you must `delete` the newer ones first. That is a real
choice, not a formality: **deleting a set discards the route back past that
point.** If you find yourself doing it during an incident, stop and think about
which point in time you actually want to land on.

#### What this guard does not cover

**The check is narrower than Proxmox's own, and the gap is not theoretical.**

| | Looks at |
|---|---|
| `snapshot-cluster.sh` recency check | `qm listsnapshot` — the VM **config** snapshot view. Only snapshots taken as *guest* snapshots. |
| Proxmox `volume_rollback_is_possible()` | `zfs list -t snapshot -r <zvol>` — **every snapshot on the zvol**, whatever created it. |

So a zvol snapshot with no matching VM config entry is **invisible to the
pre-flight but blocking to Proxmox**. Three realistic sources:

- a **`vzdump`** leftover (a failed or interrupted backup can leave its temporary
  snapshot behind),
- a **storage replication** snapshot (`__replicate_*`), if a replication job is
  ever configured for these VMs,
- a **manual `zfs snapshot`** taken on the host.

In any of those cases the pre-flight passes, all 4 VMs are stopped, and the first
`qm rollback` fails — **the exact total-outage failure the guard exists to
prevent, reached by a route the guard does not watch.**

This is documented rather than fixed, deliberately. Closing it means resolving
each VM's disks to zvol paths (parse `qm config`, resolve the storage ID through
`/etc/pve/storage.cfg`) and listing them directly: new, untested code in the one
path that runs mid-incident, in a script that has never yet been run against real
infrastructure. Failing it closed would block rollback during an outage; failing
it open would buy nothing.

**Check for foreign snapshots before taking a gate instead**, where being wrong
costs nothing:

```sh
# Every zvol snapshot on the k3s VM disks, whatever created it.
# The dataset path is resolved with `pvesm path` rather than assumed -- do not
# hardcode a pool name here, the storage ID is what the VM config actually
# references and it maps through /etc/pve/storage.cfg.
ssh root@192.168.5.1 'for id in 100 101 102 103; do
  qm config "$id" \
    | sed -n "s/^\(scsi\|virtio\|sata\|ide\)[0-9]\+: \([^,]*\).*/\2/p" \
    | while read -r volid; do
        path=$(pvesm path "$volid" 2>/dev/null) || continue
        case "$path" in
          /dev/zvol/*) zfs list -t snapshot -H -o name -r "${path#/dev/zvol/}" ;;
          *) echo "NOT-A-ZVOL $volid -> $path" ;;
        esac
      done
done' | sort
```

Cross-check that against `./snapshot-cluster.sh list`. Anything present in the
first and absent from the second is a snapshot this tool cannot see and Proxmox
will honour. Resolve it before you rely on a gate.

> This snippet has **not** been run against the host — nothing in this repo has
> been, and the disk keys, storage IDs and CD-ROM/`none` entries in `qm config`
> are read from documentation rather than from this machine. Read its output
> critically the first time: if it prints nothing at all, that is far more likely
> to be a parsing miss than four VMs with no snapshots. `NOT-A-ZVOL` lines are
> informational (a cdrom or a non-ZFS disk), not errors.

Plan Task 0 Step 2c runs this as a pre-flight, along with a check for backup and
replication jobs that could create a foreign snapshot *while* a gate is held.

If a rollback aborts with `not most recent snapshot on <volid>` naming something
`snapshot-cluster.sh list` never showed you, **this is that gap** — the script is
not broken; list the zvol's snapshots by hand and find out what made them.

#### DST

The recency check reads the timestamps `qm listsnapshot` prints, which are in the
Proxmox host's local time. During a DST fall-back fold an hour of snapshots can
compare in the wrong order; Proxmox's own check still catches that case, so the
consequence is a pre-flight that degrades to the old behaviour for one hour a
year, not one that lets something new through. (Unlike the gap above, which does
let something through.)

**Delete snapshots once a change is confirmed.** They are copy-on-write, so cost
grows with divergence; leaving them indefinitely consumes `main-pool`.
