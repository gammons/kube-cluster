# immich

Self-hosted photo library ([Immich](https://immich.app) v3.2.0), reachable **only
over Tailscale** at `http://immich.rya-scala.ts.net:2283` — there is no Ingress, no
cert-manager certificate and no public DNS record; exposure is a Tailscale L4
Service proxy created by the `tailscale.com/expose` annotation in `values.yml`. The
photo library itself lives on the NFS `nfs-bulk` pool (PVC `immich-library`, 2Ti),
the database is a hand-written single-replica PostgreSQL 17 StatefulSet
(`postgres.yml`) using Immich's own image so VectorChord is preinstalled, and
server / machine-learning / valkey come from the official Helm chart
`oci://ghcr.io/immich-app/immich-charts/immich` at **0.13.2** with the image tag
pinned to `v3.2.0` in `values.yml`. Everything is in the `immich` namespace except
`alerts.yml`, which is a `PrometheusRule` in `monitoring`.

> **In an emergency.** Database lost or corrupt →
> [Restoring the database](#restoring-the-database--unrehearsed) (start there; it is
> the only recovery path). Database volume being **resized** → [The `local-path`
> volumes can never be resized](#the-local-path-volumes-can-never-be-resized)
> instead: a resize is a destroy-and-rebuild, and the restore section documents only
> its last step. Server or Postgres down → [Monitoring](#monitoring).
> Library disk dead → [Backup posture](#backup-posture): it is not backed up, and
> re-importing from Takeout is the answer.

Everything else: [Why not CloudNativePG](#why-not-cloudnativepg) ·
[Install order](#install-order) · [Secrets](#secrets) ·
[Changing configuration](#changing-configuration) ·
[Valkey and the job queue](#valkey-and-the-job-queue) ·
[Storage notes](#storage-notes) ·
[Importing from Google Takeout](#importing-from-google-takeout)

## Why not CloudNativePG

CNPG is deliberately **not** used here: this cluster runs Kubernetes 1.29, and the
newest CNPG release that officially lists 1.29 is 1.25.x (EOL 2025-08-22) — current
CNPG supports 1.34–1.36, and its chart's `kubeVersion: ">=1.29.0-0"` means it would
*install* into an untested pairing rather than refuse. Separately, the official
Immich CNPG example uses `spec.postgresql.extensions`, which needs Kubernetes
>= 1.33 with the ImageVolume feature gate, containerd >= 2.1.0 and PostgreSQL 18;
this cluster fails all three.

**Do not "fix" this by adding CNPG.** Revisit it after the k3s 1.29 → 1.36 upgrade
(`../docs/superpowers/specs/2026-09-12-k3s-cluster-upgrade-design.md`), which is a
separate project. The plain StatefulSet is version-agnostic and needs no rework
when that lands.

## Install order

Rebuild from scratch, in this order. Every command is scoped to `local-k3s`; the
shell's default context is an unrelated production cluster. That applies to commands
written into this file as much as to commands you type, so re-check it after editing:

```bash
grep -nE '(^|[[:space:]`(])(kubectl|helm|velero)[[:space:]]' immich/README.md \
  | grep -v 'context local-k3s'
```

Expected: no output. The leading character class is the part that matters: an
unscoped command written inline inside a backtick span in a parenthetical is exactly
how one slipped past an earlier review of this file, and a pattern anchored straight
to the command name does not see it.

1. Namespace and the five persistent PVCs:

   ```bash
   kubectl --context local-k3s apply -f immich/ns.yml
   kubectl --context local-k3s apply -f immich/pvc.yml
   ```

   `immich-postgres-data` and `immich-valkey-data` will sit in `Pending`. That is
   correct: `local-path` is `WaitForFirstConsumer` and binds only once a pod
   mounts the claim. The `nfs` / `nfs-bulk` claims are `Immediate` and bind now.

2. Postgres credentials. **The imperative `create secret` below is the canonical
   workflow** — use it. The password is generated on the spot and never touches
   disk at all:

   ```bash
   PGPW=$(openssl rand -base64 32 | tr -d '/+=' | head -c 32)
   kubectl --context local-k3s create secret generic immich-postgres -n immich \
     --from-literal=POSTGRES_USER=immich \
     --from-literal=POSTGRES_PASSWORD="$PGPW" \
     --from-literal=POSTGRES_DB=immich
   ```

   `secrets.yml.example` is the **alternative**, and is kept mainly to document
   the Secret's shape (key names, namespace). Its own header tells you to copy it
   to `secrets.yml`, replace the password and apply; that also keeps the real
   value out of git (`.gitignore` blocks `secrets.yml`), but it writes a plaintext
   password to the working tree, so prefer the imperative form unless you
   specifically need a file to review.

3. Postgres StatefulSet and its headless Service:

   ```bash
   kubectl --context local-k3s apply -f immich/postgres.yml
   kubectl --context local-k3s rollout status statefulset/immich-postgres -n immich --timeout=300s
   ```

   Then prove VectorChord is loadable — this is the step that validates the whole
   database approach:

   ```bash
   kubectl --context local-k3s exec -n immich immich-postgres-0 -- psql -U immich -d immich -c "CREATE EXTENSION IF NOT EXISTS vchord CASCADE;"
   kubectl --context local-k3s exec -n immich immich-postgres-0 -- psql -U immich -d immich -c "SHOW shared_preload_libraries;"
   ```

   Expected: the extension is created, and the second command's output contains
   `vchord.so`. If `CREATE EXTENSION vchord` fails, the image tag is wrong — stop
   and fix the tag rather than working around it.

4. The Helm release (server, machine-learning, valkey). Render first, install
   second:

   ```bash
   helm template --kube-context local-k3s immich oci://ghcr.io/immich-app/immich-charts/immich --version 0.13.2 -n immich -f immich/values.yml > /tmp/immich-render.yaml
   helm --kube-context local-k3s install immich oci://ghcr.io/immich-app/immich-charts/immich --version 0.13.2 -n immich -f immich/values.yml
   ```

   `values.yml` is **overrides only** — do not vendor the upstream `values.yaml`
   into it.

5. Nightly `pg_dump`:

   ```bash
   kubectl --context local-k3s apply -f immich/pgdump-cronjob.yml
   ```

   Do not wait until 04:00 to find out whether it works:

   ```bash
   kubectl --context local-k3s create job -n immich --from=cronjob/immich-pgdump pgdump-manual-1
   # Stream the pod and block until it exits, whichever way it exits.
   # `--for=condition=complete` on its own would instead block for the full
   # timeout when the Job fails, because a failed Job never gets that condition.
   kubectl --context local-k3s logs -f -n immich --pod-running-timeout=120s job/pgdump-manual-1
   # The log is already on screen; this only turns the outcome into an exit code.
   kubectl --context local-k3s wait --for=condition=complete job/pgdump-manual-1 -n immich --timeout=30s \
     && echo "JOB COMPLETE" \
     || { echo "JOB FAILED"; kubectl --context local-k3s get job pgdump-manual-1 -n immich -o jsonpath='{"succeeded="}{.status.succeeded}{" failed="}{.status.failed}{"\n"}'; }
   kubectl --context local-k3s delete job pgdump-manual-1 -n immich
   ```

   Expected: `JOB COMPLETE`, then a `dump size:` line well above 10240 bytes,
   then `done`.

6. Prometheus alerts. `alerts.yml` declares `namespace: monitoring` in the
   manifest itself — the `PrometheusRule` must live beside Prometheus, not in
   `immich`, so do not add `-n immich` here:

   ```bash
   kubectl --context local-k3s apply -f immich/alerts.yml
   ```

7. Tailscale exposure and the API key. The Tailscale operator reconciles
   asynchronously from the Service annotations already in `values.yml`, so this
   step is a wait-and-confirm, not an apply:

   ```bash
   kubectl --context local-k3s get pods -n tailscale | grep -i immich
   curl -s -o /dev/null -w "%{http_code}\n" http://immich.rya-scala.ts.net:2283/api/server/ping
   ```

   Expected: a `ts-immich-*` pod `1/1 Running`, and `200`. Then open the UI, create
   the admin account, generate an API key (Account Settings → API Keys) and store
   it — the import Jobs read it from this Secret:

   ```bash
   kubectl --context local-k3s create secret generic immich-api -n immich --from-literal=API_KEY='<paste-key-here>'
   ```

8. Backups. The `immich` namespace is covered by the `velero-homelab-daily`
   schedule; see `../velero/README.md`. Confirm rather than assume:

   ```bash
   kubectl --context local-k3s get schedule velero-homelab-daily -n velero -o jsonpath='{.spec.template.includedNamespaces}'
   ```

## Secrets

The Postgres password is generated at deploy time and lives only in the cluster.
Retrieve it with:

```bash
kubectl --context local-k3s get secret immich-postgres -n immich \
  -o jsonpath='{.data.POSTGRES_PASSWORD}' | base64 -d; echo
```

## Changing configuration

Two traps live here, and between them they account for most of the time lost in
this deployment's history.

**Immich's admin UI is partly read-only.** The chart renders `immich.configuration`
from `values.yml` into a ConfigMap and sets `IMMICH_CONFIG_FILE=/config/immich-config.yaml`
on the server. Immich treats that file as authoritative, so everything under it is
greyed out in the admin UI: **machine-learning** (clip, facial recognition, ocr,
duplicate detection), the **storage template**, **job concurrency**
(`thumbnailGeneration.concurrency`), the **integrity checks** and the **built-in
database backup**. Changing them in the UI is not possible; change `values.yml` and
upgrade.

**A chart upgrade does not restart the server.** Chart 0.13.2 puts no config
checksum on the pod template, so a configuration-only change updates the ConfigMap
while the running process keeps serving the values it read at start-up. Nothing
fails; it just silently does not take effect. Always finish with a rollout restart:

```bash
helm --kube-context local-k3s upgrade immich oci://ghcr.io/immich-app/immich-charts/immich --version 0.13.2 -n immich -f immich/values.yml
kubectl --context local-k3s rollout restart deploy/immich-server -n immich
kubectl --context local-k3s rollout status deploy/immich-server -n immich --timeout=300s
```

Then confirm the value actually reached the container rather than assuming it did,
e.g.:

```bash
kubectl --context local-k3s exec -n immich deploy/immich-server -- cat /config/immich-config.yaml
```

## Valkey and the job queue

Valkey holds Immich's BullMQ job queue. The chart's Deployment leaves `command` and
`args` empty, so nothing here configures valkey: it runs with the **image
defaults**, which are `maxmemory 0` and `maxmemory-policy noeviction`. It therefore
**cannot self-evict** — it grows until the kernel OOM-kills it — and persistence is
RDB-only (`appendonly no`), so an OOMKill loses up to 60 s of queue writes, i.e.
queued jobs vanish mid-import.

`limits.memory` is therefore **2Gi**, a deliberate, recorded deviation from the
512Mi in the resource table of
`../docs/superpowers/specs/2026-09-12-immich-homelab-design.md`. **Do not "restore
it to spec".** Requests stay at `256Mi` so QoS stays `Burstable` and scheduling is
unchanged; only the ceiling moved. An eviction policy is not the alternative:
BullMQ requires `noeviction`, and `allkeys-lru` would silently evict queue entries
and corrupt queue state, which is worse than a visible OOMKill.

## Storage notes

### Volume map

| PVC | Class | Size | Mounted at | In Velero? |
|---|---|---|---|---|
| `immich-library` | `nfs-bulk` | 2Ti | `/data` in `immich-server` | **No** — excluded by pod annotation |
| `immich-ml-cache` | `nfs` | 10Gi | `/cache` in `immich-machine-learning` | No — excluded; model cache is rebuildable |
| `immich-pgdump` | `nfs` | 20Gi | `/dumps` **read-only** in `immich-server`; read-write in the pgdump CronJob | **Yes** — this is the point |
| `immich-postgres-data` | `local-path` | 50Gi | `/var/lib/postgresql/data` in `immich-postgres-0` | **No** — excluded; see backup posture |
| `immich-valkey-data` | `local-path` | 1Gi | `/data` in `immich-valkey` | Yes |

The library mounts at **`/data`**, not `/usr/src/app/upload` — that path does not
exist in the v3 image, and older guides that reference it are pre-v3.

`immich-pgdump` is mounted into `immich-server` purely so Velero can see it: kopia
can only read a PVC that a `Running` pod mounts, and the CronJob pod is
`Succeeded` long before the 06:00 backup. The mount is `readOnly: true` so Immich
can never write to or delete the dumps.

That is **not** why the restore below mounts the PVC in its own pod. A restore only
*reads* the dump, so `readOnly: true` would be no obstacle at all. The restore needs
its own pod because step 2 of that procedure scales `immich-server` to **0**, so
there is no server pod left to `exec` into.

### The `local-path` volumes can never be resized

`local-path` does not set `allowVolumeExpansion`, so it is false:
`immich-postgres-data` (50Gi) and `immich-valkey-data` (1Gi) **cannot be grown in
place**, ever. Growing the database volume is a destroy-and-rebuild: take a fresh
dump, scale the StatefulSet to 0 so the pod releases the claim, edit the size in
`pvc.yml`, delete the PVC (the data is gone at this point — see `reclaimPolicy`
below), re-apply `pvc.yml`, scale the StatefulSet back to 1 so `WaitForFirstConsumer`
binds a new empty volume, and only then load the dump back in. **Only that last step
is documented below** — ["Restoring the database"](#restoring-the-database--unrehearsed)
covers dump → `psql` and nothing else. The PVC surgery is not written up anywhere and
must be worked out at the time. Note the PVC is a standalone object referenced by
`claimName` (`postgres.yml`), not a `volumeClaimTemplates` entry, so it is deleted and
recreated independently of the StatefulSet. `local-path` PVs additionally use
`reclaimPolicy: Delete`, unlike `nfs` and `nfs-bulk` which are `Retain`, so deleting
one of those PVCs destroys the data immediately with no orphaned PV to recover from.
Confirm before you act:

```bash
kubectl --context local-k3s get sc -o custom-columns='NAME:.metadata.name,EXPAND:.allowVolumeExpansion,RECLAIM:.reclaimPolicy,BINDING:.volumeBindingMode'
```

### The library disk is SMR

`nfs-bulk` is backed by a single **Seagate ST8000DM004** — 8 TB, **SMR**, no
redundancy, ~15 ms average read wait. Large sequential reads and writes are fine
(the Takeout verification pass sustained ~180 MB/s). The hundreds of thousands of
small thumbnail writes are SMR's worst case, so thumbnail and machine-learning
passes run considerably slower than throughput estimates suggest. This is why
`immich.configuration.job.thumbnailGeneration.concurrency` is `1` and
`integrityChecks.checksumFiles` is disabled in `values.yml`.

### `DB_STORAGE_TYPE`

`DB_STORAGE_TYPE=SSD` in `postgres.yml`. Reasoning, and its limits:

**Device.** node-3's `/var/lib/rancher/k3s/storage` is on the root filesystem:
`/dev/mapper/ubuntu--vg-ubuntu--lv` → `sda3` → `sda`.

**The rotational flag is not usable evidence.** `sda` reports `rotational=1`,
model `QEMU HARDDISK`. That is QEMU's default for an emulated disk when the
hypervisor does not set `rotation_rate=1`; it describes the emulated device, not
the media. Do not conclude `HDD` from it.

**Synthetic dd tests — discount these.** O_DIRECT reads of `/dev/sda` measured
~300us random 4k reads and 0.8-1.8 GB/s sequential. But the LV is 504GB with only
~133GB used, and those tests read at 100GB/300GB offsets and randomly across the
first 400GB — so most blocks touched were almost certainly never written. On
LVM-thin, sparse qcow2, or a ZFS zvol (all Proxmox defaults) reads of unallocated
blocks are served from metadata with no media access, and would produce these
same numbers on a spinning disk. Recorded only so nobody re-derives them and
mistakes them for proof.

**node_exporter counters — real allocated I/O.** Boot-lifetime block-layer
counters over genuine filesystem reads. No sparse blocks, no synthetic test:

| node       | reads   | read time | avg wait | avg req |
|------------|---------|-----------|----------|---------|
| k3s-node-3 | 95,827  | 70.563s   | 736us    | 61.3 kB |
| k3s-node-2 | 339,701 | 113.370s  | 334us    | 13.1 kB |

Sub-millisecond average wait across hundreds of thousands of real reads is not
achievable by a 7200rpm spindle (~8ms per seek). Re-check any time, read-only —
port-forward the Prometheus Service rather than relying on a NodePort:

```bash
kubectl --context local-k3s -n monitoring port-forward svc/kube-prometheus-stack-prometheus 9090:9090
```

then, in another shell:

```bash
curl -s --get http://127.0.0.1:9090/api/v1/query --data-urlencode \
  'query=node_disk_read_time_seconds_total{job="node-exporter",device="sda"} / node_disk_reads_completed_total{job="node-exporter",device="sda"}'
```

**The `job="node-exporter"` selector is required.** Without it the query returns
**8** series across three jobs, not the three k3s nodes: it also picks up an
unrelated OVH cluster (`job="ovh-node-exporter"`) and the Proxmox host
(`job="proxmox-host"`, ~15 ms) — which reads as a flat contradiction of the
sub-millisecond claim above. The k3s series carry no node name, only
`instance`: `192.168.10.2` is k3s-node-1, `.3` is k3s-node-2, `.4` is k3s-node-3.
(There is also a `prometheus-external` NodePort on `30090` which is quicker to
reach, but NodePorts and node IPs rot; the port-forward above does not.)

**Why the choice is robust regardless.** `DB_STORAGE_TYPE` only sets
`random_page_cost` and `effective_io_concurrency`. It does **not** set
`max_wal_size`, despite a persistent belief that it does — the image's two
templates differ by exactly those two lines, and `max_wal_size=5GB` is identical
in both. Whether the media is NVMe or spindles behind a large flash/DRAM array
cache, Postgres observes sub-millisecond random reads and tolerates a deep queue
either way. The SSD profile matches the *observed* behaviour, which is what those
two settings model — so the decision holds even if the sparse-read caveat above
applies in full. (Postgres's own `random_page_cost=1.2` /
`effective_io_concurrency=200` are *not* evidence of anything: they are a
deterministic function of the env var we set, and only confirm it took effect.)

**Reversing it is cheap.** Change the env var in `postgres.yml` and restart the
pod: no data migration, no reinitialisation. Not a one-way door.

## Backup posture

**The photo library is not backed up, by design.** `immich-library` carries the
`backup.velero.io/backup-volumes-excludes: data` annotation on the server pod.
Google Photos is the source of truth for the imported set, and pushing a
multi-terabyte library through kopia at the ~2 MB/s uplink throttle
(`../velero/README.md`) would never complete. Accepting that is the deal: **a
failure of the single non-redundant 8 TB SMR disk loses the library**, and
re-importing from Takeout is the recovery path.

**The database is protected by `pg_dump`, not by Velero.** `immich-postgres-data`
is excluded from backup too, because a kopia filesystem copy of a running
Postgres data directory is not crash-consistent — it would restore to a corrupt or
silently-wrong cluster. Instead `pgdump-cronjob.yml` runs `pg_dump --clean
--if-exists | gzip` at 04:00 daily onto the `immich-pgdump` PVC, keeps 14 days,
and fails the job loudly if the output is under 10 KiB. That PVC *is* backed up by
Velero (see the volume map above), so the dumps reach S3.

**Immich's own built-in nightly database backup is deliberately disabled**
(`immich.configuration.backup.database.enabled: false` in `values.yml`). It
defaults to *on* and writes into `/data/backups` — which is `immich-library`, the
one volume deliberately excluded from Velero. That created a second, unmonitored
recovery path on un-backed-up storage that *looked* like safety, and put extra
small-write load on the SMR disk. **Do not re-enable it.** `immich-pgdump` is the
single documented and alerted path.

Alerting on that path is in `alerts.yml`: `ImmichPgDumpFailed` (no success in 48 h)
and `ImmichPgDumpMissing` (the metric series is absent entirely — the blind spot
the first alert cannot see, because an absent series evaluates to an empty vector
and stays silent forever).

## Restoring the database — UNREHEARSED

> **This procedure has never been executed.** It is written from the manifests, not
> from experience. It is also the *only* recovery path for the database. Rehearse
> it once against a scratch database — restore into a throwaway
> `immich_restore_test` database on the same server and compare row counts — before
> you ever need it for real, and record the outcome here.

### 1. Find a dump

They live on the `immich-pgdump` PVC. If `immich-server` is still `Running`, the
dumps are visible read-only inside it and that is the cheapest place to look:

```bash
kubectl --context local-k3s exec -n immich deploy/immich-server -- ls -la /dumps
```

**If that fails, use the throwaway Job below instead.** Do not treat the `exec` as
the only way in: the most common trigger for this whole procedure is a lost or
corrupt `immich-postgres-data`, and in that state `immich-server` is in
`CrashLoopBackOff` — so the `exec` above fails precisely when you need it most. This
Job mounts the PVC directly and depends on nothing but the PVC itself. Save it as
`/tmp/immich-dump-ls.yml`:

```yaml
---
apiVersion: batch/v1
kind: Job
metadata:
  name: immich-dump-ls
  namespace: immich
spec:
  backoffLimit: 0
  template:
    metadata:
      annotations:
        # Same reasoning as the restore Job below: a short-lived pod must not
        # drag the 20Gi dumps PVC through a PodVolumeBackup if a scheduled
        # backup overlaps it.
        backup.velero.io/backup-volumes-excludes: dumps
    spec:
      restartPolicy: Never
      dnsConfig:
        options:
          - name: ndots
            value: "1"
      containers:
        - name: ls
          image: ghcr.io/immich-app/postgres:17-vectorchord1.1.1
          command:
            # bash, NOT sh — see the note under the restore Job below.
            - /bin/bash
            - -c
            - |
              set -euo pipefail
              ls -la /dumps
          volumeMounts:
            - name: dumps
              mountPath: /dumps
              readOnly: true
      volumes:
        - name: dumps
          persistentVolumeClaim:
            claimName: immich-pgdump
```

```bash
kubectl --context local-k3s apply -f /tmp/immich-dump-ls.yml
kubectl --context local-k3s logs -f -n immich --pod-running-timeout=120s job/immich-dump-ls
kubectl --context local-k3s wait --for=condition=complete job/immich-dump-ls -n immich --timeout=30s \
  && echo "LS COMPLETE" \
  || { echo "LS FAILED"; kubectl --context local-k3s get job immich-dump-ls -n immich -o jsonpath='{"succeeded="}{.status.succeeded}{" failed="}{.status.failed}{"\n"}'; }
kubectl --context local-k3s delete job immich-dump-ls -n immich
```

The Job mounts `readOnly: true` because listing needs nothing more; it is a
deliberately inert way to read the PVC while the rest of the stack is broken.

Names are `immich-YYYYMMDD-HHMMSS.sql.gz`, 14 days are retained, and anything
under ~10 KiB should not exist (the CronJob fails rather than writing one) — but
check the size anyway. If the PVC itself is gone, restore it from Velero first, then
run the listing Job above:

```bash
velero --kubecontext local-k3s restore create --from-backup <name> --include-namespaces immich
```

Note that Velero only restores the *file*: nothing loads it into Postgres. That is
what the rest of this procedure does.

### 2. Stop everything that writes

Immich v3 runs the microservices worker inside the server pod, so this one
scale-down is sufficient. Machine-learning holds no database credentials
(deliberately — it parses untrusted uploads), and valkey holds none either.

```bash
kubectl --context local-k3s scale deploy immich-server -n immich --replicas=0
kubectl --context local-k3s rollout status deploy/immich-server -n immich --timeout=300s
```

### 3. Run the restore Job

Save the manifest below as `/tmp/immich-restore.yml`, editing `DUMP` to the file
chosen in step 1. It mounts `immich-pgdump` in its own pod: the server's `/dumps`
mount is read-only, and the server is down anyway.

```yaml
---
apiVersion: batch/v1
kind: Job
metadata:
  name: immich-restore
  namespace: immich
spec:
  backoffLimit: 0
  template:
    metadata:
      annotations:
        # Keep this Job out of any scheduled backup that overlaps it. Note it
        # is NOT "already captured via immich-server" — step 2 scaled that to 0,
        # so right now nothing is backing this volume up. The exclusion is still
        # correct: this pod lives for minutes during an outage, and letting it
        # become the mount point that drags 20Gi through kopia would slow the
        # restore without protecting anything the 04:00 dump cycle won't.
        backup.velero.io/backup-volumes-excludes: dumps
    spec:
      restartPolicy: Never
      dnsConfig:
        options:
          - name: ndots
            value: "1"
      containers:
        - name: restore
          image: ghcr.io/immich-app/postgres:17-vectorchord1.1.1
          env:
            - name: DUMP
              value: immich-YYYYMMDD-HHMMSS.sql.gz
            - name: PGHOST
              value: immich-postgres.immich.svc.cluster.local
            - name: PGUSER
              valueFrom:
                secretKeyRef:
                  name: immich-postgres
                  key: POSTGRES_USER
            - name: PGPASSWORD
              valueFrom:
                secretKeyRef:
                  name: immich-postgres
                  key: POSTGRES_PASSWORD
            - name: PGDATABASE
              valueFrom:
                secretKeyRef:
                  name: immich-postgres
                  key: POSTGRES_DB
          command:
            # bash, NOT sh — see the note below this manifest.
            - /bin/bash
            - -c
            - |
              set -euo pipefail
              test -f "/dumps/${DUMP}"
              gzip -t "/dumps/${DUMP}"
              echo "restoring ${DUMP} into ${PGDATABASE} on ${PGHOST}"
              zcat "/dumps/${DUMP}" | psql -v ON_ERROR_STOP=1 --single-transaction
              echo "restore finished"
          volumeMounts:
            - name: dumps
              mountPath: /dumps
              readOnly: true
      volumes:
        - name: dumps
          persistentVolumeClaim:
            claimName: immich-pgdump
```

```bash
kubectl --context local-k3s apply -f /tmp/immich-restore.yml
```

Credentials come from the same `immich-postgres` Secret the `pg_dump` CronJob uses,
so they cannot drift apart. The dump was written with `pg_dump --clean --if-exists`,
so it drops and recreates objects inside the existing `immich` database — you do
**not** need to drop the database first.

**`/bin/sh` in `ghcr.io/immich-app/postgres` is dash, not bash.** `set -o pipefail`
is a special-builtin failure under dash and aborts the script on line 1; that
silently broke the `pg_dump` CronJob once. Anything you run in this image must use
`/bin/bash`.

### 4. Watch it — read the output, not just the exit code

The Job sets `backoffLimit: 0`, so a **failed** Job never receives the `complete`
condition, and waiting only on `complete` would block for the entire hour before
telling you anything — during an outage. Stream the log instead. It returns as soon
as the pod exits either way, and it puts the failure text on screen, which is what
you actually have to read:

```bash
kubectl --context local-k3s logs -f -n immich --pod-running-timeout=120s job/immich-restore
kubectl --context local-k3s wait --for=condition=complete job/immich-restore -n immich --timeout=30s \
  && echo "RESTORE COMPLETE" \
  || { echo "RESTORE FAILED"; kubectl --context local-k3s get job immich-restore -n immich -o jsonpath='{"succeeded="}{.status.succeeded}{" failed="}{.status.failed}{"\n"}'; }
```

`logs -f` uses no job control, so it behaves identically in `bash` and `zsh` and is
unaffected by anything you already had backgrounded — the port-forward under
[Monitoring](#monitoring), say. **Do not "improve" this into backgrounded
`wait --for=condition=…` calls reaped with `wait -n` and `kill %1 %2`.** That shape
is broken: `wait -n` does not exist in zsh (it fails with `job not found: -n`), so
execution falls straight through mid-restore with no indication anything was
skipped; and `%1`/`%2` are absolute job numbers, so with any pre-existing background
job they name the operator's own process and `kill` SIGTERMs it. The second command
above only classifies the outcome, since the log has already streamed — that is why
its timeout is 30s rather than an hour. If `logs -f` returns at once complaining the
container `is waiting to start`, the pod simply had not started yet; run it again.

Expected: `RESTORE COMPLETE`, then `restore finished` in the log.
`--single-transaction` with `ON_ERROR_STOP=1` makes the load all-or-nothing, so a
half-applied restore should not be possible. If it aborts, read the first `ERROR:`
line — do not re-run blindly, and do not start the server against a database whose
restore failed.

### 5. Verify before scaling back up

Table names in v3.2.0 are **singular**: `asset`, `album` and `"user"` — the last is
a reserved word and must be quoted. `assets` / `albums` / `users` do not exist and
return "relation does not exist", which is easy to misread as a failed restore.

```bash
kubectl --context local-k3s exec -n immich immich-postgres-0 -- psql -U immich -d immich -c 'SELECT (SELECT count(*) FROM asset) AS assets, (SELECT count(*) FROM album) AS albums, (SELECT count(*) FROM "user") AS users;'
```

Expected: counts consistent with the library, and in particular a non-zero user
count. Zero users means Immich will present the initial-admin-signup screen on
start-up, which is the clearest sign the restore did not land.

### 6. Scale back up and confirm the application agrees

```bash
kubectl --context local-k3s scale deploy immich-server -n immich --replicas=1
kubectl --context local-k3s rollout status deploy/immich-server -n immich --timeout=300s
kubectl --context local-k3s logs -n immich deploy/immich-server --tail=100
curl -s -o /dev/null -w "%{http_code}\n" http://immich.rya-scala.ts.net:2283/api/server/ping
```

Expected: `200`, and no repeating database errors in the log. If the dump predates
the running Immich image, the server runs its migrations on start-up — normal, but
watch the log rather than assuming.

### 7. Clean up, and expect queue noise

```bash
kubectl --context local-k3s delete job immich-restore -n immich
```

Valkey still holds whatever BullMQ queued before the restore, and those jobs may
reference rows that no longer exist. Transient job errors immediately afterwards
are expected. If they persist, restart valkey and accept the loss of the queued
jobs:

```bash
kubectl --context local-k3s rollout restart deploy/immich-valkey -n immich
```

## Importing from Google Takeout

The bulk import lives in `import/` and is described in Tasks 10–12 of
`../docs/superpowers/plans/2026-09-12-immich-homelab.md`. It is transient work:
these manifests are run once and then deleted, which is why they are split out from
the steady-state config above.

- `import/takeout-pvc.yml` — a 1500Gi `nfs-bulk` PVC (`immich-takeout`) holding the
  raw Takeout archives. Deleted once the import is confirmed good.
- `import/immich-go-job.yml` — **two** Jobs, run one at a time. Never apply the
  whole file at once; select a single Job from it.
  - `immich-go-import-zips` uploads the 53 `takeout-*.zip` chunks with
    `immich-go upload from-google-photos`.
  - `immich-go-import-standalone` handles the one 19.5 GB MP4 that Google served
    *outside* the zips because it exceeds the 10 GB split size, re-paired with the
    sidecar JSON that *is* inside chunk 035. A `*.zip` glob cannot see that file,
    and filename-date parsing is wrong for it (the filename is ~40 minutes off the
    sidecar's `photoTakenTime` and carries no GPS).

Both Jobs download and checksum-verify the official `immich-go` v0.32.0 release
tarball into a stock Debian image — immich-go publishes **binaries only**, and
`ghcr.io/simulot/immich-go` does not exist. Both carry
`backup.velero.io/backup-volumes-excludes: takeout`, which is **mandatory**: the
`immich` namespace is in the Velero schedule, and without it the next nightly
backup would try to push ~1.5 TiB through the ~2 MB/s throttle.

Machine learning is disabled in `values.yml` for the duration of the import (clip,
facial recognition, ocr and duplicate detection all default to `true` in v3.2.0;
ocr in particular runs CPU-only inference on every asset and there is no GPU here).
Re-enabling it is Task 12 — edit `values.yml`, then follow "Changing
configuration" above: upgrade the release **and** rollout restart.

## Monitoring

`alerts.yml` is a `PrometheusRule` in the `monitoring` namespace, picked up by
kube-prometheus-stack via the `prometheus: kube-prometheus-stack` label:

| Alert | Fires when |
|---|---|
| `ImmichServerDown` | `up` is 0 for the server for 10 m |
| `ImmichPostgresDown` | the StatefulSet has no ready replica for 10 m |
| `ImmichPgDumpFailed` | no successful `immich-pgdump` run in 48 h |
| `ImmichPgDumpMissing` | the CronJob's success metric is absent for 6 h |
| `ImmichLibraryVolumeFilling` | `immich-library` is over 85 % full for 30 m |

`metrics.enabled: true` in `values.yml` exposes Immich's own metrics. After a
deliberate delete-and-recreate of the CronJob, `ImmichPgDumpMissing` fires until
the first scheduled 04:00 run completes; that is expected, not a fault.

```bash
kubectl --context local-k3s get prometheusrule immich-alerts -n monitoring
kubectl --context local-k3s get cronjob,jobs -n immich
```
