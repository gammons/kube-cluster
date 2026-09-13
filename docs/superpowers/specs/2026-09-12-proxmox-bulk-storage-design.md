# Proxmox Bulk Storage: 8TB Provisioning + Mirror Repair

**Date:** 2026-09-12
**Host:** `pve` (root@192.168.5.1), Proxmox VE 8.4.11, kernel 6.8.12-13-pve, ZFS 2.2.8-pve1
**Cluster:** k3s v1.29.4, 4 VMs (100 controller, 101-103 workers), kubectl context `local-k3s`

## Original Goal

Provision a new 8TB spinning disk (`/dev/sda`) and make it available to VMs and
the k3s cluster, ultimately to host Immich.

Investigation surfaced two higher-priority problems that were fixed first.

---

## Part 1: Degraded ZFS Mirror (fixed)

### Starting state

`main-pool` (2x 2TB Crucial T500 NVMe mirror) was **DEGRADED** with one half missing.
All four k3s VM disks and the 586G `k3s-nfs` dataset were running on a single drive
with zero redundancy. Degraded since at least the 2026-08-09 scrub.

```
main-pool                 DEGRADED
  mirror-0                DEGRADED
    nvme2n1               ONLINE
    16054423747030243868  UNAVAIL   was /dev/nvme1n1p1
```

### Root cause

Not a hardware failure. The pool was created using **kernel device names** instead of
`/dev/disk/by-id/` paths. On a reboot, NVMe enumeration shuffled: the 2TB Crucial that
was `nvme1n1` became `nvme0n1`, and `nvme1n1` became the 238GB Toshiba boot drive.

The stored vdev config held a stale `path` but a **correct `devid`**:

```
children[1]:
    guid: 16054423747030243868
    path:  '/dev/nvme1n1p1'                            <-- STALE (now the boot drive)
    devid: 'nvme-CT2000T500SSD8_23504743DB46-part1'    <-- CORRECT
    not_present: 1
    DTL: 116693
```

Proxmox imports via `zpool import -c /etc/zfs/zpool.cache -aN`, which trusts `path`.
ZFS looked at `/dev/nvme1n1p1`, found LVM boot data, and marked the vdev `not_present`.

### Why the obvious fixes failed

| Attempt | Result | Reason |
|---|---|---|
| `zpool replace` | Hard refusal | Target's label claims membership in an *imported* pool |
| `zpool replace -f` | Hard refusal | `-f` only overrides *potentially* in-use devices, not active-pool membership |
| `zpool reopen` | No-op (exit 0) | Reopens via stored path; nothing to reopen |
| `zpool online <guid>` | "remains in faulted state" | Same stale path, no rescan |

**Key constraint:** device discovery by `devid` happens **only at `zpool import`**.
Every other command operates inside the already-loaded config.

### Fix applied

Required stopping the VMs (zvols held the pool open). Host root is on
`/dev/mapper/pve-root` (LVM, separate disk), so a failed import could not brick the host.

```sh
qm shutdown 100 101 102 103     # 102/103 required `qm stop` - no guest agent
systemctl stop nfs-server
zpool export main-pool
zpool import -d /dev/disk/by-id main-pool
zpool clear main-pool
```

### Verification

- Resilver: **3.72G in 9 seconds** (DTL delta resilver, not a full 1.58T rebuild)
- Full scrub: **`repaired 0B in 00:28:09 with 0 errors`**
- 2 checksum errors during resilver, repaired from the good half — expected when
  readmitting a stale mirror member. SMART on both drives: 0 media errors, 100% spare.
- Both vdevs now tracked by stable firmware identifiers; `zpool.cache` updated,
  so boot-time import is now rename-proof.

```
main-pool                                      ONLINE
  mirror-0                                     ONLINE
    nvme-CT2000T500SSD8_235047441B36_1         ONLINE 0 0 0
    nvme-eui.000000000000000100a075234743db46  ONLINE 0 0 0
```

Total cluster downtime: ~12 minutes, mostly the two hung guests.

### Drive wear note

Both NVMe drives: ~20,200 power-on hours, **41-42% rated life used**, ~510TB written,
within 1% of each other. Same model, same batch, same workload — correlated failure
risk. Plan a staggered replacement rather than replacing both at once.

---

## Part 2: Capacity Reclaim (done)

`main-pool` was at **92% capacity** (134G free). ZFS write performance degrades badly
past ~80%.

`pmbot-data` PVC accounted for 526G of the 586G `k3s-nfs` dataset.

> **Note:** this PVC was **actively being written** at the time (the `recorder`
> deployment was streaming live Polymarket/Coinbase/Binance tick data; newest file was
> seconds old). This was flagged explicitly; deletion was confirmed as an informed
> decision. 526G / 11,925 files / 2026-07-05..2026-09-12 was destroyed and is
> **not recoverable** — market tick data cannot be backfilled.

Actions:
1. `kubectl scale deploy/recorder -n pmbot --replicas=0`, confirmed writes stopped
2. Deleted PVC `pmbot-data`, then the PV (required manually — `nfs` SC uses
   `reclaimPolicy: Retain`, which is why the data had survived earlier cleanup)
3. Removed the directory from the host
4. `pmbot-curated` (55G, used by the miner CronJobs) left **untouched**

K8s manifests preserved at `~/pmbot-teardown/` so the pipeline can be recreated.

#### Fallout: broke the Velero backup (2026-09-13)

**`dev-box/dev-box-0` mounted the deleted directory as a raw NFS volume**, in a
different namespace, with no PVC involved:

```yaml
- name: pmbot-raw-ro
  nfs: {server: 192.168.5.1, path: /main-pool/k3s-nfs/pmbot-pmbot-data-pvc-6cc16a1a-...}
```

`dev-box` is one of the five namespaces Velero backs up, so the next run failed:

```
velero-homelab-daily-20260913060058   PartiallyFailed   06:00:58 -> 06:33:05   errors: 1
  Failed  dev-box/dev-box-0  vol=pmbot-raw-ro  "timeout on preparing PVB"
```

The mount used the **`hard`** NFS option, so accessing the dead path blocked instead of
erroring — the run took 33 minutes instead of the usual 5-8, and any process in the dev
box touching `/data/raw` would have hung too.

**Why the pre-delete check missed it:** consumers were searched with
`kubectl get pods -n pmbot` filtered on *PVC references*. Since PVCs are
namespace-scoped, the conclusion was that only `pmbot` pods could consume the data. But
a raw `nfs:` volume references the server path directly and is not namespace-scoped in
any meaningful way. The evidence was visible beforehand — `vol=pmbot-raw-ro` appeared in
the `PodVolumeBackup` listing — and was misread as pmbot-namespace noise.

**Correct pre-delete check** — all namespaces, all workload kinds, match on the path
string, not on PVC references:

```sh
for kind in pods deployments statefulsets daemonsets cronjobs jobs replicasets; do
  kubectl get $kind -A -o json | jq -r --arg p "<path-fragment>" \
    '.items[] | select(tostring | contains($p)) | "\(.kind) \(.metadata.namespace)/\(.metadata.name)"'
done
```

**Resolution:** removed the `pmbot-raw-ro` volume and its `/data/raw` mount from the
`dev-box` StatefulSet via a guarded JSON patch (using `test` ops so a shifted index
fails safely rather than deleting the wrong element). `pmbot-curated-ro` → `/data/curated`
retained and verified readable. Pod recreated clean (3/3, 0 restarts); a manual backup
then completed **466/466 items in 2.5 minutes** with all 14 volumes `Completed`.

Backup of the StatefulSet: `~/proxmox-monitoring/devbox-fix/dev-box-sts.backup.yaml`.

### Result

| Metric | Before | After |
|---|---|---|
| Capacity | 87% | **58%** |
| Free | 240G | **766G** |
| Fragmentation | 70% | **45%** |
| `k3s-nfs` | 586G | 60.7G |

---

## Part 3: Bulk Pool on the 8TB (done)

### Drive characteristics

`ST8000DM004-2U9188` (serial `ZR16KFGD`) — Seagate BarraCuda 3.5, **SMR**
(shingled, drive-managed), 5400 rpm, 512e/4096p sectors.

SMR implications: good at large sequential write-once workloads; hostile to sustained
random writes (band read-modify-write). Immich originals suit it; Postgres and
thumbnail generation do not.

### Pool creation

```sh
zpool create -o ashift=12 \
  -O compression=lz4 -O atime=off -O xattr=sa -O dnodesize=auto \
  -O mountpoint=/bulk-pool \
  bulk-pool /dev/disk/by-id/ata-ST8000DM004-2U9188_ZR16KFGD

zfs create bulk-pool/k3s-bulk
zfs set recordsize=1M      bulk-pool/k3s-bulk
zfs set primarycache=metadata bulk-pool/k3s-bulk
zfs set logbias=throughput bulk-pool/k3s-bulk
```

Rationale:
- **by-id path** — avoids repeating the Part 1 failure mode
- **`ashift=12`** — matches 4096-byte physical sectors; permanent, unchangeable
- **`recordsize=1M`** — turns a photo write into a few large sequential I/Os instead
  of many small ones. The single most important SMR setting.
- **`primarycache=metadata`** — RAM is oversubscribed (VMs 40G + ARC max 31G vs 61G
  physical). Prevents streaming media reads from evicting VM working sets.
  Revert with `zfs inherit primarycache bulk-pool/k3s-bulk` if undesirable.
- **Single-disk vdev, deliberately** — a second 8TB can later be added in place via
  `zpool attach`, converting to a mirror with no data migration. Use
  **`zpool attach -s`** for a sequential rebuild (much faster on SMR), then scrub.

### Measured performance

| Operation | Throughput |
|---|---|
| Sustained write (24GB, `conv=fdatasync`) | **192 MB/s** |
| Sustained read (caches dropped) | **204 MB/s** |
| Via NFS from a pod (200MB) | 106 MB/s |

Earlier readings of 7.5 GB/s (all-zeros, defeated by lz4) and 599 MB/s (2GB, absorbed
by the 4GB `zfs_dirty_data_max`) were measurement artifacts, not real throughput.

**Expectation for bulk import:** 192 MB/s is a *burst* figure — 24GB did not exhaust
the drive's 20-40GB CMR cache, and it wrote to fast outer tracks. A multi-TB Google
Takeout import will settle to roughly **40-100 MB/s**. Import in batches; do not scrub
concurrently.

---

## Part 4: Kubernetes Exposure (done)

Reused the existing pattern rather than introducing new machinery.

**NFS export** (`/etc/exports`, backup at `/etc/exports.bak-2026-09-12`):
```
/bulk-pool/k3s-bulk 192.168.0.0/16(rw,sync,no_subtree_check,no_root_squash)
```

**Second provisioner** — Helm release alongside the existing `nfs-provisioner`:
```sh
helm install nfs-bulk-provisioner \
  nfs-subdir-external-provisioner/nfs-subdir-external-provisioner \
  -n nfs-provisioner --version 4.0.18 \
  --set nfs.server=192.168.5.1 \
  --set nfs.path=/bulk-pool/k3s-bulk \
  --set storageClass.name=nfs-bulk \
  --set storageClass.reclaimPolicy=Retain \
  --set storageClass.archiveOnDelete=true \
  --set storageClass.allowVolumeExpansion=true
```

The differing release name yields a unique `provisionerName`
(`cluster.local/nfs-bulk-provisioner-...`), so the two StorageClasses don't conflict.

Resulting StorageClasses:

| Name | Backing | Reclaim |
|---|---|---|
| `local-path` (default) | node-local, NVMe | Delete |
| `nfs` | `main-pool` NVMe mirror | Retain |
| `nfs-bulk` | `bulk-pool` 8TB SMR | Retain |

**Verified end-to-end:** provisioned a PVC, wrote 200MB from a pod, confirmed via `df`
that bytes landed on `bulk-pool/k3s-bulk`, confirmed `main-pool` untouched, cleaned up.

Rejected alternative: PCIe/disk passthrough to a single VM — would pin Immich to one
node and lose scheduling flexibility.

---

## Part 5: Immich Placement (superseded — Immich is deployed)

> **This section is no longer authoritative.** Immich was designed and deployed
> separately; see `2026-09-12-immich-homelab-design.md` and the plan at
> `../plans/2026-09-12-immich-homelab.md`. Where the two documents disagree, the
> Immich spec wins.

Deployed state as of 2026-09-13 (namespace `immich`):

| PVC | StorageClass | Size | Backing |
|---|---|---|---|
| `immich-library` | `nfs-bulk` | 2Ti | 8TB SMR — originals, video, **and thumbnails** |
| `immich-ml-cache` | `nfs` | 10Gi | NVMe mirror |
| `immich-pgdump` | `nfs` | 20Gi | NVMe mirror |
| `immich-postgres-data` | `local-path` | 50Gi | NVMe, node-local |
| `immich-valkey-data` | `local-path` | 1Gi | NVMe, node-local |

The storage split this document originally proposed was adopted, with **one
deliberate divergence**: thumbnails live on `nfs-bulk` alongside originals rather than
on the NVMe `nfs` class. The Immich spec made that call; this document previously
recommended otherwise and is corrected here so the two do not contradict.

The load-bearing constraints still hold and are why the layout looks like this:
- **`upload/` and `library/` must share one volume.** Immich finalizes imports with a
  rename; split across volumes turns every upload into a full cross-device copy.
- **Postgres must not run on NFS.** Immich documentation warns against network storage
  for the database; `local-path` gives correct fsync semantics at the cost of pinning
  the pod to one node (normal for a single-instance DB).

### Backup posture

Primary copy is a **single disk with no parity**. The accepted mitigation is that
Google Photos retains the upstream copy. Two caveats:
- Google Photos only retains true originals on "Original quality"; "Storage saver"
  re-compresses.
- This stops being a backup the moment photos are deleted from Google.

`immich` **was** added to the Velero schedule (now 6 namespaces), and an
`immich-pgdump` CronJob dumps Postgres to a `nfs`-backed PVC that is mounted read-only
into `immich-server` so Velero captures it.

Note that Velero's fs-backup only covers volumes attached to **running pods**, and the
schedule still covers only 6 of ~31 namespaces — see Part 6.

---

## Part 6: Outstanding Issues

### Monitoring (FIXED — see Part 7)

The mirror was degraded for weeks and nothing reported it. There were **two
independent silent-alerting failures**, both now diagnosed; see Part 7 for the fix.

**Failure 1 — ZED email bounced.** Detection worked; delivery did not:

```
zed[1229]: eid=2 class=statechange pool='main-pool' vdev=nvme1n1p1 vdev_state=UNAVAIL

postfix/smtp: to=<grant@grant.dev>, relay=aspmx.l.google.com:25, status=bounced
  (550-5.7.1 [96.245.213.71] The IP you're using to send mail is not authorized
   to send email directly to our servers)
```

Postfix attempts direct-to-MX delivery from a residential IP; Gmail rejects it.
`relayhost` is empty, `inet_interfaces = loopback-only`, no `/etc/pve/notifications.cfg`.
**Left unfixed deliberately** — superseded by Prometheus/Slack, which works.

**Failure 2 — Alertmanager discarded all homelab alerts.** Even with metrics, nothing
would have been delivered. Prometheus sets `externalLabels: {cluster: homelab}`, and
the route table contained:

```yaml
- matchers: [cluster =~ "homelab|local|local-k3s"]
  receiver: "null"
```

Only `cluster="ovh"` reached Slack. 24 alerts were firing and being silently dropped.

### Other follow-ups

| Item | Priority | Notes |
|---|---|---|
| Dead-man's switch for `Watchdog` | High | Needs an external heartbeat URL — see Part 7 |
| ~~Widen route to `severity=warning`~~ | **Done 2026-09-13** | Helm rev 13. Prompted by the incident above: the *warning* `VeleroBackupPartiallyFailed` named the exact cause at 06:33, but was discarded, while the *critical* `VeleroNoSuccessfulBackup` (26h threshold) only fired hours later. The more diagnostic signal was the one being thrown away. |
| Triage the 21 pre-existing warnings now reaching Slack | Medium | 5 of 6 groups trace to the `servicelb`/MetalLB conflict (`KubePodNotReady`, `KubeDaemonSetRolloutStuck` on `svclb-lb-unifi`) — fixed by Phase 0 of the upgrade spec |
| Rotate Slack webhook | Medium | Stored plaintext in Helm values; exposed if committed to git |
| Install `qemu-guest-agent` on VMs 100-103 | Medium | No agent configured; ACPI shutdown failed on 102/103, forcing hard stops. `apt install qemu-guest-agent` + `qm set <id> --agent 1` |
| `recorder` had 131 restarts | Medium | Pre-existing instability, unrelated to storage |
| `miner-polymarket` repeatedly OOMKilled | Medium | `miner-kalshi` succeeds; needs a memory limit review |
| Namespaces `signoz`, `longhorn-system` stuck `Terminating` 2y+ | Low | Pre-existing cruft, likely finalizers |
| Staggered NVMe replacement | Low | Both at 42% wear with correlated aging |
| ARC vs VM memory oversubscription | Low | 40G VMs + 31G ARC max vs 61G physical |

### Scrub schedule

`/etc/cron.d/zfsutils-linux` scrubs **all** pools on the 2nd Sunday monthly, so
`bulk-pool` is covered automatically. Both pools scrub concurrently, which is
acceptable as they're separate physical devices. Expect 6-10 hours for `bulk-pool`
once substantially full.

---

## Part 7: Host Monitoring via In-Cluster Prometheus (done)

Chosen over fixing postfix: Alertmanager already had a **working Slack webhook**, so
no SMTP relay or new credentials were needed.

### Known limitation: circular dependency

Prometheus runs in k3s VMs **hosted by the machine it monitors**.

| Scenario | Detected |
|---|---|
| Pool DEGRADED but still serving (the actual 2026-09-12 incident) | Yes |
| Capacity/fragmentation/SMART wear/scrub overdue | Yes |
| Device read/write/checksum errors | Yes |
| Pool unimportable, host down, VMs dead | **No — Prometheus dies too** |

**Mitigation (not yet implemented):** the `Watchdog` alert fires continuously by
design and currently routes to `null`. Point it at an external heartbeat
(healthchecks.io, Better Stack, Grafana Cloud) so *absence* of the heartbeat becomes
the alert. This is the only way to cover total failure without a second monitoring host.

### 1. Host exporter

```sh
apt-get install -y prometheus-node-exporter   # 1.5.0, bookworm
```

The Debian package also pulls `prometheus-node-exporter-collectors`, which provides
**smartmon** and **nvme** timers for free, and patches
`--collector.textfile.directory` to `/var/lib/prometheus/node-exporter` by default.

**Critical finding:** node_exporter's built-in zfs collector reads
`/proc/spl/kstat/zfs`, which has **no pool health field**. It produced 440
`node_zfs_*` metrics and not one reported `DEGRADED`. Installing node_exporter alone
would have recreated the original blind spot.

Gap filled by `/usr/local/bin/zfs-textfile-collector.sh`, run every 5 min by
`prometheus-zfs.timer`, emitting:

```
zpool_health{pool}                              0=ONLINE 1=DEGRADED 2=FAULTED 3=OFFLINE 4=UNKNOWN
zpool_capacity_percent{pool}
zpool_fragmentation_percent{pool}
zpool_device_errors{pool,device,type}            read|write|cksum
zpool_scrub_age_seconds{pool}
zpool_scrub_in_progress{pool}
zpool_vdevs_total{pool}
zpool_collector_last_run_timestamp_seconds       staleness canary
```

Writes to a temp file then `mv` (atomic) so node_exporter never reads a partial file.

**Verified by building a throwaway file-backed mirror and degrading it:**
`ONLINE → 0`, `zpool offline` → `DEGRADED → 1`. Asserting the metric merely exists
would not have proven detection.

### 2. Scrape config

Appended to the existing `additional-scrape-configs` secret (14 → 15 jobs):

```yaml
- job_name: 'proxmox-host'
  static_configs:
    - targets: ['192.168.5.1:9100']
      labels: {cluster: 'homelab', environment: 'homelab', service: 'node-exporter',
               node: 'pve', role: 'hypervisor'}
  scrape_interval: 30s
```

The `cluster: homelab` label is what makes the routing below work.

### 3. Alert rules

`PrometheusRule monitoring/proxmox-host-alerts` — 15 rules in 3 groups. The
Prometheus `ruleSelector` is `{}` so no special labels are required.

| Group | Critical | Warning |
|---|---|---|
| `proxmox-zfs` | ZpoolDegraded, ZpoolDeviceErrors, ZpoolCapacityCritical | ZpoolCapacityHigh, ZpoolScrubOverdue, ZpoolNeverScrubbed, ZfsCollectorStale |
| `proxmox-disks` | SmartDeviceUnhealthy, NvmeCriticalWarning, NvmeMediaErrors, NvmeSpareLow, DiskPendingSectors | NvmeWearHigh, DiskReallocatedSectors |
| `proxmox-host-up` | ProxmoxHostExporterDown | — |

Metric names were verified against live output first; initial guesses
(`smartmon_percentage_used_raw_value`, `nvme_info`) did not exist. Real names:
`nvme_percentage_used_ratio` (0-1, not percent), `nvme_available_spare_ratio`,
`nvme_critical_warning_total`, `nvme_media_errors_total`,
`smartmon_device_smart_healthy`, `smartmon_current_pending_sector_raw_value`,
`smartmon_reallocated_sector_ct_raw_value`. Alerts written against non-existent
metrics fail silently — always confirm the name.

`ZpoolDegraded` carries the remediation in its annotation: export + `import -d
/dev/disk/by-id`, **not** `zpool replace`.

### 4. Alertmanager routing

Route added **before** the homelab catch-all (Alertmanager takes the first match):

```yaml
- matchers:
  - cluster =~ "homelab|local|local-k3s"
  - severity = "critical"
  receiver: slack-alerts
  repeat_interval: 4h
```

Applied via `helm upgrade` (rev 11 → 12, chart pinned 81.5.0) because
`alertmanager.config` lives in Helm values — editing the Secret directly would be
overwritten on the next upgrade. No pods restarted.

Warnings still route to `null` intentionally, so the 24 pre-existing firing warnings
did not flood `#alerts`.

### End-to-end verification

Full chain proven with a real degraded pool, not a synthetic alert:

1. `zpool offline` on a throwaway mirror → collector wrote `zpool_health 1`
2. Prometheus scraped it: `zpool_health{pool="alerttest-pool", cluster="homelab"} => 1`
3. Rule went `pending` → **`firing`** after the 2m threshold
4. Alertmanager: `receivers=slack-alerts` (not null)
5. `alertmanager_notifications_total{integration="slack"} = 1`,
   all `alertmanager_notifications_failed_total{...} = 0`
6. Test pool destroyed → alert resolved and cleared

### Artifacts

| Path | Contents |
|---|---|
| `/usr/local/bin/zfs-textfile-collector.sh` | on pve |
| `/etc/systemd/system/prometheus-zfs.{service,timer}` | on pve |
| `~/proxmox-monitoring/proxmox-host-alerts.yaml` | PrometheusRule |
| `~/proxmox-monitoring/scrape-configs.backup.yaml` | pre-change scrape configs |
| `~/proxmox-monitoring/kps-values.backup.yaml` | pre-change Helm values (rollback) |

Rollback: `helm upgrade ... -f kps-values.backup.yaml`, or `helm rollback
kube-prometheus-stack 11 -n monitoring`.

---

## Final State

```
NAME        SIZE  ALLOC   FREE   FRAG    CAP   HEALTH
bulk-pool  7.27T   864K  7.27T     0%     0%   ONLINE
main-pool  1.81T  1.06T   766G    45%    58%   ONLINE
```

Both pools ONLINE, all vdevs addressed by stable firmware identifiers, all four k3s
nodes `Ready`, `nfs-bulk` StorageClass verified working end-to-end.
