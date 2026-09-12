# Immich on the local-k3s homelab cluster

Date: 2026-09-12
Status: approved, ready for implementation planning
Cluster: `local-k3s` (k3s v1.29.4)

## Goal

Self-host Immich on the homelab k3s cluster and bulk-import a 300 GB–1 TB
Google Photos library from Google Takeout. Access is Tailscale-only. Google
Photos remains the system of record for the media itself.

## Context: live cluster state

The repo `README.md` is stale. These are the verified facts the design rests on.

| Property | Value |
|---|---|
| Kubernetes | k3s **v1.29.4**, EOL since 2025-02-28 |
| Datastore | SQLite/kine, single server (`k3s-controller`), no etcd |
| Container runtime | containerd 1.7.15 |
| Workers | k3s-node-1/2/3, 8 CPU and 12 GB RAM each |
| Controller | cordoned + `NoExecute` tainted, runs no workloads |
| Node load | node-2 at 83% memory requests / 216% memory limits; node-1 29%; node-3 33% |
| GPU | none |
| Ingress | Traefik v2.10.5 at 192.168.20.1; `tailscale` IngressClass also present |
| Tailscale | operator v1.76.6, tailnet `rya-scala.ts.net` |
| Cert manager | v1.14.5, `letsencrypt-prod` ClusterIssuer (unused by this design) |
| Storage classes | `local-path` (default, node-local), `nfs`, `nfs-bulk` |
| Monitoring | kube-prometheus-stack 81.5.0 in `monitoring`; all Prometheus selectors are `{}` |
| Backups | Velero 1.18.1, schedule `velero-homelab-daily`, ~2 MB/s kopia cap |
| Scale | 111 running pods, 26 namespaces, ~30 Helm releases |

**Longhorn has been removed** (`longhorn-system` is `Terminating`, zero pods).
Any Longhorn reference in the repo is obsolete.

NFS backing, both classes served from `192.168.5.1`:

| Class | Export | Size | Free |
|---|---|---|---|
| `nfs` | `/main-pool/k3s-nfs` | 720 GB | 660 GB |
| `nfs-bulk` | `/bulk-pool/k3s-bulk` | 7.1 TB | 7.1 TB (empty) |

Both use `nfs-subdir-external-provisioner` 4.0.2 with `archiveOnDelete: "true"`
and `reclaimPolicy: Retain`. Deleting a PVC archives its directory rather than
destroying it. Neither enforces quota, so PVC sizes are bookkeeping only.

`bulk-pool` is a single 8 TB disk with no redundancy.

### Deployment conventions this design follows

There is no GitOps controller. Apps are deployed by hand with
`helm install -f values.yml` and `kubectl apply -f`, documented in a
per-directory `README.md` that doubles as a runbook. One app directory per
commit, lowercase imperative commit messages, `.yml` extension.

## Requirements

1. Immich reachable only over Tailscale; nothing published to the public internet.
2. Import a Google Takeout export preserving capture dates, album membership, and GPS.
3. Photos themselves need no backup — Google Photos is retained as the source of truth.
4. The Immich database (albums, faces, metadata) must be backed up, since it
   represents work that is expensive to regenerate.
5. Follow existing repo conventions; no new cluster-wide operators.

## Key constraint: no CloudNativePG

Immich v3.2.0 requires PostgreSQL >= 14 with either VectorChord
(`>=0.3 <2`) or pgvector (`>=0.5 <1`). Chart 0.13.2 no longer bundles a
database.

CloudNativePG was evaluated and **rejected** on this cluster:

- CNPG 1.30.x supports Kubernetes 1.34–1.36. The newest CNPG release that
  officially lists k8s 1.29 is **1.25.x**, EOL since 2025-08-22.
- The Helm chart declares `kubeVersion: ">=1.29.0-0"`, so CNPG 1.30 would
  *install*, but the pairing is untested by upstream.
- The official Immich CNPG example uses `spec.postgresql.extensions`, which
  requires Kubernetes >= 1.33 with the ImageVolume feature gate,
  containerd >= 2.1.0, and PostgreSQL 18. This cluster fails all three.

Adopting an unsupported operator/Kubernetes pairing contradicts the reason to
choose an operator at all. **Decision: plain StatefulSet using Immich's own
Postgres image**, which is version-agnostic and needs no rework after a future
k3s upgrade.

The k3s upgrade (1.29 → 1.36, seven sequential minor hops, plus cert-manager,
Tailscale operator, and MetalLB upgrades) is real and worth doing, but is a
**separate project** and explicitly out of scope here.

## Architecture

Namespace `immich`. Four workloads, no operators.

| Component | Source | Version |
|---|---|---|
| `immich-server` | `oci://ghcr.io/immich-app/immich-charts/immich` | chart 0.13.2, `image.tag: v3.2.0` |
| `immich-machine-learning` | same chart | v3.2.0, CPU-only image |
| `immich-valkey` | same chart, `valkey.enabled: true` | chart default |
| `immich-postgres` | hand-written StatefulSet | `ghcr.io/immich-app/postgres:17-vectorchord1.1.1` |

Chart 0.13.2 has no separate `microservices` component; Immich v3 merged it
into the server. Only the server mounts the library volume.

The old HTTP chart repo (`immich-app.github.io/immich-charts`) has been
removed. The OCI registry is the only source.

`image.tag` must be pinned explicitly — the chart does not track Immich releases.

## Storage

| PVC | Class | Access | Size | Purpose |
|---|---|---|---|---|
| `immich-library` | `nfs-bulk` | RWX | 2Ti | Photo and video originals plus generated thumbnails |
| `immich-ml-cache` | `nfs` | RWX | 10Gi | CLIP and face model cache |
| `immich-valkey-data` | `local-path` | RWO | 1Gi | Job queue |
| `immich-postgres-data` | `local-path` | RWO | 50Gi | Database |
| `immich-pgdump` | `nfs` | RWX | 20Gi | Nightly `pg_dump` output |
| `immich-takeout` | `nfs-bulk` | RWX | 1.5Ti | Takeout archives, deleted after import |

The chart defaults `immich-ml-cache` and `immich-valkey-data` to `emptyDir`.
Both are overridden: an `emptyDir` model cache re-downloads roughly 2 GB of
models on every restart, and an `emptyDir` job queue loses queued work mid-import,
forcing a re-run of "Missing" jobs across the whole library.

The library PVC must be created before install and referenced via
`immich.persistence.library.existingClaim` — the chart cannot create it.

Postgres is on `local-path` rather than NFS deliberately. Postgres over NFS is
a correctness hazard (fsync semantics, lock files) as well as a performance
problem for VectorChord index work.

## Database

Plain StatefulSet, `replicas: 1`, pinned to **k3s-node-3** via `nodeSelector`.
node-3 is the least loaded worker; node-2 is excluded because it is already at
83% memory requests.

`local-path` PersistentVolumes carry node affinity, so the pod would return to
its node regardless. The `nodeSelector` is stated explicitly so the constraint
is visible in the manifest rather than emergent from volume binding.

Configuration:

- `POSTGRES_USER`, `POSTGRES_PASSWORD`, `POSTGRES_DB` from a Secret created
  out-of-band. A `secrets.yml.example` is committed; the real file is
  git-ignored, following the `velero/` pattern.
- `DB_STORAGE_TYPE`: `SSD` or `HDD`, set from the verified disk type of node-3.
- PostgreSQL **17**. Immich ships 14 and 18 is available; 17 is chosen as a
  well-tested middle. Changing major version later requires a dump and restore.
- `shared_preload_libraries=vchord.so` is preset in the image; no custom
  `postgresql.conf` is required.

### DNS

Pods on this cluster resolve with
`search default.svc.cluster.local svc.cluster.local cluster.local lan rya-scala.ts.net`
and `ndots:5`. This triggers the musl/Alpine resolution bug that Immich's
Kubernetes documentation warns about. Two mitigations, both applied:

1. Internal service references (`DB_HOSTNAME`, `REDIS_HOSTNAME`,
   `IMMICH_MACHINE_LEARNING_URL`) use fully-qualified
   `*.immich.svc.cluster.local` names, bypassing the search list.
2. `dnsConfig` sets `ndots: 1` for the external lookups machine-learning makes
   when downloading models. Applied to all chart-managed components via the
   chart's top-level `defaultPodOptions`, and repeated in the hand-written
   Postgres StatefulSet pod spec, which the chart does not manage.

## Access

No Ingress, no cert-manager, no public DNS. Two annotations on the
`immich-server` Service, matching the existing `monitoring/loki-tailscale`
pattern:

```yaml
tailscale.com/expose: "true"
tailscale.com/hostname: "immich"
```

Result: `immich.rya-scala.ts.net:2283`.

A Service-level L4 proxy is chosen over a Tailscale Ingress because there is no
HTTP body-size limit to tune for large video uploads, and Tailscale already
encrypts at the transport layer, so a plain-HTTP listener is acceptable.

**Accepted trade-off:** mobile background backup requires the Tailscale VPN to
stay connected on the phone. On iOS especially, a dropped VPN pauses backups
until it reconnects.

## Resources and machine learning

| Workload | Requests | Limits |
|---|---|---|
| server | 1 CPU / 2Gi | 4 CPU / 6Gi |
| machine-learning | 1 CPU / 2Gi | 4 CPU / 8Gi |
| postgres | 1 CPU / 2Gi | 4 CPU / 6Gi |
| valkey | 100m / 256Mi | 1 CPU / 512Mi |

Scheduling is explicit rather than left to the scheduler:

- `immich-postgres` — `nodeSelector` pinned to **k3s-node-3** (see Database).
- `immich-server`, `immich-machine-learning`, `immich-valkey` — a
  `nodeAffinity` rule excluding **k3s-node-2**, leaving node-1 and node-3
  eligible. node-2 is already at 83% memory requests and 216% memory limits;
  adding an 8Gi-limit ML pod there invites eviction of existing workloads.

The control plane node is already unschedulable and needs no exclusion.

CPU-only CLIP embedding and face detection across a library of this size is
expected to take **1–3 days** of background processing. Both are therefore
disabled for the import, declaratively via `immich.configuration`:

```yaml
machineLearning:
  clip: { enabled: false }
  facialRecognition: { enabled: false }
```

They are re-enabled after import, followed by the admin UI "Missing" jobs.
Running ML concurrently with a large ingest slows both and obscures failures.

Video transcoding is not a significant concern despite the absence of a GPU:
Google Takeout exports are predominantly H.264/MP4 and Immich's default policy
only transcodes non-conforming files. Transcode concurrency stays at 1.

The **storage template is set before import** to
`{{y}}/{{y}}-{{MM}}-{{dd}}/{{filename}}`. Enabling or changing it after import
triggers a bulk move of every file on disk.

## Import pipeline

### Stage 1 — delivery

Request the Takeout export with **"Add to Drive"** rather than a download link.
At 10 GB per chunk this is 30–100 files; emailed links expire in about seven
days and have limited retry attempts.

### Stage 2 — land on the NAS

An `rclone` Job copies Drive to the `immich-takeout` PVC on `nfs-bulk`. Drive
credentials come from a Secret. The 7.1 TB pool holds the takeout and the
imported library concurrently without difficulty.

### Stage 3 — import

`immich-go` **v0.32.0** in a Job pod:

- mounts `immich-takeout` read-only
- targets `http://immich-server.immich.svc.cluster.local:2283`, staying
  in-cluster rather than routing through Tailscale
- consumes the chunked zip files directly
- parses Google's `.json` sidecars to preserve capture dates, album membership,
  and GPS, which a naive file upload would lose
- `restartPolicy: Never`, no `activeDeadlineSeconds` — this runs for hours
- a `--dry-run` pass precedes the real run

Exact immich-go flag syntax is verified against the v0.32.0 release during
implementation rather than fixed here.

Prerequisite: the admin user exists and an API key has been generated.

### Stage 4 — post-import

Re-enable CLIP and facial recognition, run the "Missing" jobs, verify asset
counts against Google Photos, then delete the `immich-takeout` PVC.
`archiveOnDelete: "true"` preserves the directory even then.

## Backup

`immich` is added to `includedNamespaces` in the `velero-homelab-daily`
schedule in `velero/values.yml`. Velero backs up the volumes of every pod it
includes, so **four** volumes are excluded via the
`backup.velero.io/backup-volumes-excludes` pod annotation:

- **`immich-library`** (server pod) — at the documented ~2 MB/s kopia cap, a
  500 GB library takes roughly three days per backup. Google Photos is the
  backup. This mirrors the existing exclusion of `pmbot-raw-ro`.
- **`immich-takeout`** (import Job pods) — up to 1.5 TB of archives that exist
  only transiently and are already a copy of data held by Google. Without this
  exclusion the first nightly backup after the import starts would attempt to
  upload the entire takeout.
- **`immich-ml-cache`** (machine-learning pod) — re-downloadable model weights.
- **`immich-postgres-data`** (postgres pod) — excluded for correctness, not
  bandwidth. A kopia filesystem copy of a running Postgres data directory is
  not crash-consistent and is not reliably restorable. Including it would
  create a false sense of safety.

What Velero does capture: the namespace's Kubernetes objects, `immich-pgdump`,
and `immich-valkey-data`.

Because the exclusion annotation must be present on the **pod template** of
every workload that mounts an excluded volume, the import Jobs carry it from
the moment they are authored, not added retroactively.

Database durability comes from a nightly `CronJob` running `pg_dump` into
`immich-pgdump` with roughly 14 days of retention. Losing the database means
re-importing and re-running ML — expensive, but not data loss, which is
proportionate to dumps rather than replication.

## Monitoring

`immich.metrics.enabled: true` creates ServiceMonitors. The Prometheus
instance has empty `serviceMonitorSelector`, `serviceMonitorNamespaceSelector`,
`ruleSelector`, and `ruleNamespaceSelector`, so resources in the `immich`
namespace are discovered with no additional labelling.

A `PrometheusRule` follows the `velero/velero-alerts.yml` convention
(`prometheus: kube-prometheus-stack`, `role: alert-rules`) covering:

- Immich server unavailable
- the `pg_dump` CronJob failing — a silently failing backup is the usual way
  one discovers there was no backup
- `bulk-pool` crossing a fill threshold

## Repository layout

Following the `jellyfin` / `uptime-kuma` / `velero` convention:

```
immich/
  README.md              runbook: install commands, secret creation, import steps, troubleshooting
  ns.yml                 Namespace
  pvc.yml                the six PVCs
  postgres.yml           Postgres StatefulSet + Service
  values.yml             Helm overrides only, not a vendored upstream values file
  secrets.yml.example    template; real secrets git-ignored
  pgdump-cronjob.yml     nightly pg_dump
  alerts.yml             PrometheusRule
  import/
    rclone-job.yml       Drive to NAS
    immich-go-job.yml    Takeout to Immich
  .gitignore             secrets.yml, rclone.conf
```

`velero/values.yml` is also modified to add the `immich` namespace.

## Open items to verify during implementation

1. Whether node-3's underlying Proxmox storage is flash, determining
   `DB_STORAGE_TYPE`.
2. Actual Takeout size once the export completes, confirming PVC sizing.
3. immich-go v0.32.0 command syntax against its current documentation.

## Out of scope

- The k3s 1.29 → 1.36 upgrade and the associated cert-manager, Tailscale
  operator, and MetalLB upgrades.
- Offsite or redundant backup of the photo library itself.
- Migrating to CloudNativePG, which becomes viable after the cluster upgrade.
- Public internet exposure.
