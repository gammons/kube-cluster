# Immich on local-k3s Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Deploy Immich to the `local-k3s` cluster with Tailscale-only access, then bulk-import a 300 GB–1 TB Google Photos library from Google Takeout.

**Architecture:** The official Immich Helm chart (OCI, 0.13.2) provides server, machine-learning, and valkey. PostgreSQL is a hand-written single-replica StatefulSet using Immich's own image, which ships VectorChord preinstalled — CloudNativePG is unusable on this cluster's EOL Kubernetes 1.29. The photo library lives on an NFS-backed `nfs-bulk` PVC; the database lives on node-local `local-path`. Access is a Tailscale L4 Service proxy, not an Ingress.

**Tech Stack:** k3s v1.29.4, Helm (OCI registry), Immich v3.2.0, PostgreSQL 17 + VectorChord 1.1.1, Valkey, nfs-subdir-external-provisioner, Tailscale operator, Velero, kube-prometheus-stack, immich-go v0.32.0.

**Spec:** `docs/superpowers/specs/2026-09-12-immich-homelab-design.md`

## Global Constraints

Every task's requirements implicitly include these.

- **Cluster context is always `local-k3s`.** Every `kubectl` and `helm` command must pass `--context local-k3s` / `--kube-context local-k3s`. The shell's default context is a different, unrelated production cluster.
- **Chart:** `oci://ghcr.io/immich-app/immich-charts/immich` version **0.13.2**. The old HTTP repo `immich-app.github.io/immich-charts` is removed and must not be used.
- **Immich image tag:** pinned to **`v3.2.0`**. The chart does not track Immich releases; an unpinned tag is a defect.
- **Postgres image:** `ghcr.io/immich-app/postgres:17-vectorchord1.1.1`.
- **Namespace:** `immich`.
- **Storage classes:** `nfs-bulk` (library, takeout), `nfs` (ml-cache, pgdump), `local-path` (postgres, valkey). Never place Postgres on NFS.
- **`local-path` is `WaitForFirstConsumer`.** Those PVCs stay `Pending` until a pod mounts them. This is correct behaviour, not a failure. The `nfs` and `nfs-bulk` classes are `Immediate` and bind right away.
- **Node placement:** Postgres pinned to `k3s-node-3`. All other Immich workloads must avoid `k3s-node-2` (83% memory requests, 216% memory limits).
- **Values files contain overrides only.** Do not vendor the upstream `values.yaml`. (The `jellyfin/values.yml` full-copy style is legacy; follow `uptime-kuma/values.yml` instead.)
- **File extension is `.yml`**, matching repo convention.
- **Secrets never enter git.** Real secrets are created via `kubectl create secret`; only `.example` templates are committed, with `immich/.gitignore` enforcing it.
- **No Ingress, no cert-manager, no public DNS.** Exposure is Tailscale only.
- **Commit style:** lowercase, imperative, terse (`add immich`). No conventional-commit prefixes.

## Verification Discipline

This is infrastructure configuration, not application code, so there is no unit-test cycle. The equivalent discipline applies instead:

**No task is complete until its stated verification command produces its stated expected output.** "The YAML applied without error" is not verification. "The pod is Running and the API returns 200" is.

---

## File Structure

```
immich/
  .gitignore              secrets.yml, rclone.conf — enforces the no-secrets rule
  README.md               runbook: install order, secret creation, import, troubleshooting
  ns.yml                  Namespace
  pvc.yml                 5 persistent PVCs (takeout PVC is separate, it is transient)
  postgres.yml            Postgres StatefulSet + headless Service
  secrets.yml.example     template for the Postgres secret
  values.yml              Helm overrides for the immich chart
  pgdump-cronjob.yml      nightly pg_dump
  alerts.yml              PrometheusRule
  import/
    takeout-pvc.yml       transient 1.5Ti PVC, deleted after import
    immich-go-job.yml     Takeout -> Immich
```

Modified outside `immich/`: `velero/values.yml` (add the `immich` namespace to the backup schedule).

Rationale for the split: `import/` is transient work that gets run once and then deleted, so separating it keeps the steady-state config readable. Everything else is one file per concern, matching the repo's existing per-app layout.

---

## Task 1: Scaffold, namespace, and persistent PVCs

**Files:**
- Create: `immich/.gitignore`
- Create: `immich/ns.yml`
- Create: `immich/pvc.yml`

**Interfaces:**
- Consumes: nothing.
- Produces: namespace `immich`; PVCs `immich-library`, `immich-ml-cache`, `immich-pgdump` (bound immediately), `immich-postgres-data`, `immich-valkey-data` (Pending until mounted).

- [ ] **Step 1: Create `immich/.gitignore`**

```
secrets.yml
rclone.conf
```

- [ ] **Step 2: Create `immich/ns.yml`**

```yaml
---
apiVersion: v1
kind: Namespace
metadata:
  name: immich
```

- [ ] **Step 3: Create `immich/pvc.yml`**

```yaml
---
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: immich-library
  namespace: immich
spec:
  storageClassName: nfs-bulk
  accessModes:
    - ReadWriteMany
  resources:
    requests:
      storage: 2Ti
---
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: immich-ml-cache
  namespace: immich
spec:
  storageClassName: nfs
  accessModes:
    - ReadWriteMany
  resources:
    requests:
      storage: 10Gi
---
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: immich-pgdump
  namespace: immich
spec:
  storageClassName: nfs
  accessModes:
    - ReadWriteMany
  resources:
    requests:
      storage: 20Gi
---
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: immich-postgres-data
  namespace: immich
spec:
  storageClassName: local-path
  accessModes:
    - ReadWriteOnce
  resources:
    requests:
      storage: 50Gi
---
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: immich-valkey-data
  namespace: immich
spec:
  storageClassName: local-path
  accessModes:
    - ReadWriteOnce
  resources:
    requests:
      storage: 1Gi
```

- [ ] **Step 4: Apply**

```bash
kubectl --context local-k3s apply -f immich/ns.yml
kubectl --context local-k3s apply -f immich/pvc.yml
```

- [ ] **Step 5: Verify binding states**

```bash
kubectl --context local-k3s get pvc -n immich
```

Expected — note the two deliberate `Pending` entries:

```
immich-library         Bound     ...  2Ti    RWX   nfs-bulk
immich-ml-cache        Bound     ...  10Gi   RWX   nfs
immich-pgdump          Bound     ...  20Gi   RWX   nfs
immich-postgres-data   Pending                     local-path
immich-valkey-data     Pending                     local-path
```

If `immich-postgres-data` or `immich-valkey-data` shows `Bound` at this stage, something is wrong with the assumption that `local-path` is `WaitForFirstConsumer` — stop and re-check `kubectl --context local-k3s get sc local-path -o yaml`.

If any `nfs`/`nfs-bulk` PVC is `Pending`, check the provisioner: `kubectl --context local-k3s logs -n nfs-provisioner -l app=nfs-subdir-external-provisioner --tail=50`.

---

## Task 2: Determine disk type for Postgres tuning

**Files:** none (investigation only — result feeds Task 3).

**Interfaces:**
- Consumes: nothing.
- Produces: the value for `DB_STORAGE_TYPE`, either `SSD` or `HDD`.

This is its own task because the answer changes a value baked into Task 3, and getting it wrong means Postgres is mistuned for the entire life of the deployment.

- [ ] **Step 1: Find the backing device for `/var/lib/rancher/k3s/storage` on node-3**

```bash
kubectl --context local-k3s debug node/k3s-node-3 -it --image=busybox:1.36 -- \
  sh -c 'df /host/var/lib/rancher/k3s/storage 2>/dev/null || df /host'
```

Note the device name (for example `/dev/sda1` → base device `sda`).

- [ ] **Step 2: Read the rotational flag**

```bash
kubectl --context local-k3s debug node/k3s-node-3 -it --image=busybox:1.36 -- \
  cat /host/sys/block/sda/queue/rotational
```

Substitute the base device from Step 1 for `sda`.

Expected: `0` means non-rotational → use `SSD`. `1` means spinning → use `HDD`.

- [ ] **Step 3: Record the decision**

Write the chosen value into `immich/README.md` as a one-line note so the reasoning is not lost. These are Proxmox VMs, so the guest-visible rotational flag reflects what the hypervisor advertises; if it reports `1` but the underlying Proxmox storage is known to be flash, trust the physical reality and use `SSD`.

- [ ] **Step 4: Clean up debugger pods**

```bash
kubectl --context local-k3s delete pod -n default -l app=node-debugger --ignore-not-found
kubectl --context local-k3s get pods -n default | grep node-debugger
```

Expected: no running debugger pods. (The cluster already has ~15 stale `Completed` debugger pods from previous sessions; leaving more is untidy but harmless.)

---

## Task 3: Postgres StatefulSet

**Files:**
- Create: `immich/secrets.yml.example`
- Create: `immich/postgres.yml`

**Interfaces:**
- Consumes: `immich-postgres-data` PVC (Task 1); `DB_STORAGE_TYPE` value (Task 2).
- Produces: Service `immich-postgres.immich.svc.cluster.local:5432`; database `immich`, user `immich`; Secret `immich-postgres` with keys `POSTGRES_USER`, `POSTGRES_PASSWORD`, `POSTGRES_DB`.

A StatefulSet rather than a Deployment specifically because a Deployment's rolling update can briefly run two pods against one RWO volume. StatefulSet terminates before creating, which is required for Postgres safety.

- [ ] **Step 1: Create `immich/secrets.yml.example`**

```yaml
# Copy to secrets.yml, replace the password, and apply.
# secrets.yml is git-ignored. Do not commit real values.
---
apiVersion: v1
kind: Secret
metadata:
  name: immich-postgres
  namespace: immich
type: Opaque
stringData:
  POSTGRES_USER: immich
  POSTGRES_PASSWORD: CHANGE_ME
  POSTGRES_DB: immich
```

- [ ] **Step 2: Create the real secret with a generated password**

```bash
PGPW=$(openssl rand -base64 32 | tr -d '/+=' | head -c 32)
kubectl --context local-k3s create secret generic immich-postgres -n immich \
  --from-literal=POSTGRES_USER=immich \
  --from-literal=POSTGRES_PASSWORD="$PGPW" \
  --from-literal=POSTGRES_DB=immich
echo "password stored in secret; retrieve with the command in README"
```

- [ ] **Step 3: Create `immich/postgres.yml`**

Replace `__DB_STORAGE_TYPE__` with the value from Task 2 before applying.

```yaml
---
apiVersion: v1
kind: Service
metadata:
  name: immich-postgres
  namespace: immich
spec:
  clusterIP: None
  selector:
    app: immich-postgres
  ports:
    - name: postgres
      port: 5432
      targetPort: 5432
---
apiVersion: apps/v1
kind: StatefulSet
metadata:
  name: immich-postgres
  namespace: immich
spec:
  serviceName: immich-postgres
  replicas: 1
  selector:
    matchLabels:
      app: immich-postgres
  template:
    metadata:
      labels:
        app: immich-postgres
      annotations:
        backup.velero.io/backup-volumes-excludes: data
    spec:
      nodeSelector:
        kubernetes.io/hostname: k3s-node-3
      dnsConfig:
        options:
          - name: ndots
            value: "1"
      securityContext:
        fsGroup: 999
      containers:
        - name: postgres
          image: ghcr.io/immich-app/postgres:17-vectorchord1.1.1
          imagePullPolicy: IfNotPresent
          ports:
            - name: postgres
              containerPort: 5432
          envFrom:
            - secretRef:
                name: immich-postgres
          env:
            - name: DB_STORAGE_TYPE
              value: "__DB_STORAGE_TYPE__"
          volumeMounts:
            - name: data
              mountPath: /var/lib/postgresql/data
          resources:
            requests:
              cpu: "1"
              memory: 2Gi
            limits:
              cpu: "4"
              memory: 6Gi
          readinessProbe:
            exec:
              command: ["pg_isready", "-U", "immich", "-d", "immich"]
            initialDelaySeconds: 15
            periodSeconds: 10
          livenessProbe:
            exec:
              command: ["pg_isready", "-U", "immich", "-d", "immich"]
            initialDelaySeconds: 60
            periodSeconds: 30
      volumes:
        - name: data
          persistentVolumeClaim:
            claimName: immich-postgres-data
```

**Do not override `PGDATA`.** The image bakes in `PGDATA=/var/lib/postgresql/data` and uses a custom entrypoint (`immich-docker-entrypoint.sh`) plus a custom config at `/etc/postgresql/postgresql.conf` — that config is what preloads `vchord.so`. The usual reason to relocate `PGDATA` to a subdirectory is a `lost+found` on a freshly formatted block device; `local-path` volumes are bind-mounted directories and have none, so the override would add risk against a custom entrypoint for no benefit.

`fsGroup: 999` is belt-and-braces only — the image has no `USER` directive, so it starts as root and drops privileges itself.

The `backup.velero.io/backup-volumes-excludes: data` annotation is present from the start, per the spec — a filesystem copy of a live Postgres is not reliably restorable.

- [ ] **Step 4: Apply**

```bash
kubectl --context local-k3s apply -f immich/postgres.yml
```

- [ ] **Step 5: Verify the pod is Running and the PVC bound**

```bash
kubectl --context local-k3s rollout status statefulset/immich-postgres -n immich --timeout=300s
kubectl --context local-k3s get pvc immich-postgres-data -n immich
kubectl --context local-k3s get pod -n immich -l app=immich-postgres -o wide
```

Expected: rollout succeeds; PVC now `Bound`; pod `1/1 Running` on **k3s-node-3**.

- [ ] **Step 6: Verify VectorChord is actually present and loadable**

This is the step that proves the whole database approach works. Do not skip it.

```bash
kubectl --context local-k3s exec -n immich immich-postgres-0 -- \
  psql -U immich -d immich -c "CREATE EXTENSION IF NOT EXISTS vchord CASCADE;"

kubectl --context local-k3s exec -n immich immich-postgres-0 -- \
  psql -U immich -d immich -c "SELECT extname, extversion FROM pg_extension ORDER BY extname;"
```

Expected: the extension list includes `vchord` at version `1.1.1` and `vector`. Immich accepts VectorChord `>=0.3 <2`, so `1.1.1` satisfies it.

```bash
kubectl --context local-k3s exec -n immich immich-postgres-0 -- \
  psql -U immich -d immich -c "SHOW shared_preload_libraries;"
```

Expected: output contains `vchord.so`.

If `CREATE EXTENSION vchord` fails, the image tag is wrong — stop and re-verify it against the Global Constraints rather than working around it.

- [ ] **Step 7: Commit**

```bash
git add immich/.gitignore immich/ns.yml immich/pvc.yml immich/postgres.yml immich/secrets.yml.example
git commit -m "add immich namespace, volumes and postgres"
```

Confirm `immich/secrets.yml` is **not** in the commit: `git show --stat HEAD`.

---

## Task 4: Immich Helm release

**Files:**
- Create: `immich/values.yml`

**Interfaces:**
- Consumes: Postgres Service and Secret (Task 3); PVCs `immich-library`, `immich-ml-cache`, `immich-valkey-data` (Task 1).
- Produces: Deployments `immich-server`, `immich-machine-learning`, `immich-valkey`; Services `immich-server:2283`, `immich-machine-learning:3003`, `immich-valkey:6379`; ConfigMap `immich-immich-config`.

All values keys below were verified by rendering chart 0.13.2 — `server.service.main.annotations`, `defaultPodOptions.dnsConfig`, and `immich.persistence.library.existingClaim` all take effect as written.

- [ ] **Step 1: Create `immich/values.yml`**

> **THIS SNIPPET IS NO LONGER AUTHORITATIVE. `immich/values.yml` in the repo is.**
>
> This block records what Task 4 originally created. Later waves added, and the
> deployed file now contains, things this snippet does not:
> - `valkey.resources` (spec's resource table; without it valkey runs BestEffort
>   and is first OOM-killed while holding the job queue)
> - `machineLearning.ocr` and `machineLearning.duplicateDetection` set false
>   (Immich v3.2.0 defaults both to true — CPU inference on a GPU-less cluster)
> - `backup.database.enabled: false` (Immich's built-in 02:00 dump otherwise
>   writes to the Velero-excluded library volume, creating an unmonitored
>   recovery path)
> - `job.thumbnailGeneration.concurrency: 1` and
>   `integrityChecks.checksumFiles.enabled: false` (the library sits on an SMR
>   drive; the nightly full-library checksum never finishes)
>
> Rebuilding from this snippet alone produces a materially worse deployment.
> Copy the repo file instead.
>
> Also note: chart 0.13.2 puts **no config checksum on the pod template**, so
> `helm upgrade` does NOT restart the server when only `immich.configuration`
> changes. The process keeps running the old config while the ConfigMap shows
> the new one. Always follow a config-only change with
> `kubectl --context local-k3s rollout restart deploy/immich-server -n immich`.

```yaml
---
# Shared across every Immich component.
defaultPodOptions:
  dnsConfig:
    options:
      # Cluster search list is "... lan rya-scala.ts.net" with ndots:5, which
      # triggers the musl/Alpine resolution bug Immich documents. Internal
      # references below are fully qualified; this fixes external lookups.
      - name: ndots
        value: "1"
  affinity:
    nodeAffinity:
      requiredDuringSchedulingIgnoredDuringExecution:
        nodeSelectorTerms:
          - matchExpressions:
              # k3s-node-2 is at 83% memory requests / 216% memory limits.
              - key: kubernetes.io/hostname
                operator: NotIn
                values: ["k3s-node-2"]

controllers:
  main:
    containers:
      main:
        image:
          tag: v3.2.0
        env:
          REDIS_HOSTNAME: immich-valkey.immich.svc.cluster.local
          IMMICH_MACHINE_LEARNING_URL: http://immich-machine-learning.immich.svc.cluster.local:3003
          DB_HOSTNAME: immich-postgres.immich.svc.cluster.local
          DB_PORT: "5432"
          DB_DATABASE_NAME: immich
          DB_USERNAME:
            valueFrom:
              secretKeyRef:
                name: immich-postgres
                key: POSTGRES_USER
          DB_PASSWORD:
            valueFrom:
              secretKeyRef:
                name: immich-postgres
                key: POSTGRES_PASSWORD

immich:
  metrics:
    enabled: true
  persistence:
    library:
      existingClaim: immich-library
  configuration:
    storageTemplate:
      enabled: true
      hashVerificationEnabled: true
      template: "{{y}}/{{y}}-{{MM}}-{{dd}}/{{filename}}"
    machineLearning:
      # Disabled for the bulk import; re-enabled in Task 11.
      clip:
        enabled: false
      facialRecognition:
        enabled: false

valkey:
  enabled: true
  persistence:
    data:
      enabled: true
      type: persistentVolumeClaim
      existingClaim: immich-valkey-data

server:
  service:
    main:
      annotations:
        tailscale.com/expose: "true"
        tailscale.com/hostname: "immich"
  controllers:
    main:
      pod:
        annotations:
          backup.velero.io/backup-volumes-excludes: data
      containers:
        main:
          resources:
            requests:
              cpu: "1"
              memory: 2Gi
            limits:
              cpu: "4"
              memory: 6Gi

machine-learning:
  controllers:
    main:
      pod:
        annotations:
          backup.velero.io/backup-volumes-excludes: cache
      containers:
        main:
          resources:
            requests:
              cpu: "1"
              memory: 2Gi
            limits:
              cpu: "4"
              memory: 8Gi
  persistence:
    cache:
      enabled: true
      type: persistentVolumeClaim
      existingClaim: immich-ml-cache
```

The storage template's `{{y}}` placeholders are safe in a values file — Helm does not Go-template values files, verified by rendering.

- [ ] **Step 2: Render locally before installing**

```bash
helm template immich oci://ghcr.io/immich-app/immich-charts/immich \
  --version 0.13.2 -n immich -f immich/values.yml > /tmp/immich-render.yaml
echo "exit=$?"
grep -c "kind:" /tmp/immich-render.yaml
```

Expected: exit 0. Inspect that the library volume references `immich-library` and the tailscale annotations are on the `immich-server` Service:

```bash
grep -A3 "tailscale.com/expose" /tmp/immich-render.yaml
grep -B2 -A2 "claimName: immich-library" /tmp/immich-render.yaml
```

- [ ] **Step 3: Install**

```bash
helm --kube-context local-k3s install immich \
  oci://ghcr.io/immich-app/immich-charts/immich \
  --version 0.13.2 -n immich -f immich/values.yml
```

- [ ] **Step 4: Verify all pods reach Running**

```bash
kubectl --context local-k3s get pods -n immich -o wide
```

Expected: `immich-server`, `immich-machine-learning`, `immich-valkey`, `immich-postgres-0` all `Running`, and **none** scheduled on `k3s-node-2`.

- [ ] **Step 5: Verify the database schema was created**

This proves Immich connected to Postgres and ran its migrations — the highest-risk integration in the deployment.

```bash
kubectl --context local-k3s logs -n immich -l app.kubernetes.io/name=server --tail=100
```

Expected: migration output and a listening message, with no repeated connection errors.

```bash
kubectl --context local-k3s exec -n immich immich-postgres-0 -- \
  psql -U immich -d immich -c "\dt" | head -30
```

Expected: Immich's tables exist. v3.2.0 names them in the singular after the `StandardizeNames` migration — `asset`, `album`, `user`, and others — not the pre-v3 plurals.

- [ ] **Step 6: Verify the API responds in-cluster**

```bash
kubectl --context local-k3s run curltest -n immich --rm -i --restart=Never \
  --image=curlimages/curl:8.10.1 -- \
  -s -o /dev/null -w "%{http_code}\n" \
  http://immich-server.immich.svc.cluster.local:2283/api/server/ping
```

Expected: `200`.

- [ ] **Step 7: Commit**

```bash
git add immich/values.yml
git commit -m "add immich helm values"
```

---

## Task 5: Verify Tailscale exposure

**Files:** none (verification of Task 4's annotations).

**Interfaces:**
- Consumes: annotated `immich-server` Service (Task 4).
- Produces: a confirmed reachable URL for the mobile app and for Task 11.

Separate from Task 4 because the Tailscale operator reconciles asynchronously and depends on tailnet-side state (ACLs, device approval) that is outside the cluster. A failure here is a different problem with a different fix.

- [ ] **Step 1: Confirm the operator created a proxy pod**

```bash
kubectl --context local-k3s get pods -n tailscale | grep -i immich
```

Expected: a pod named like `ts-immich-<suffix>-0`, `1/1 Running`. It may take 30–60 seconds to appear.

If nothing appears, check the operator: `kubectl --context local-k3s logs -n tailscale deploy/operator --tail=50`.

- [ ] **Step 2: Confirm the device registered on the tailnet**

```bash
tailscale status | grep -i immich
```

Expected: a device named `immich`. If the tailnet requires manual device approval, approve it in the admin console before continuing.

- [ ] **Step 3: Verify reachability over the tailnet**

```bash
curl -s -o /dev/null -w "%{http_code}\n" http://immich.rya-scala.ts.net:2283/api/server/ping
```

Expected: `200`.

- [ ] **Step 4: Load the web UI and create the admin account**

Open `http://immich.rya-scala.ts.net:2283` in a browser. Complete the initial admin signup.

- [ ] **Step 5: Generate an API key**

In the UI: Account Settings → API Keys → New API Key. Store it for Task 10.

```bash
kubectl --context local-k3s create secret generic immich-api -n immich \
  --from-literal=API_KEY='<paste-key-here>'
```

- [ ] **Step 6: Verify the key works**

```bash
curl -s -H "x-api-key: $(kubectl --context local-k3s get secret immich-api -n immich \
  -o jsonpath='{.data.API_KEY}' | base64 -d)" \
  http://immich.rya-scala.ts.net:2283/api/users/me
```

Expected: JSON describing the admin user.

---

## Task 6: Nightly pg_dump CronJob

**Files:**
- Create: `immich/pgdump-cronjob.yml`

**Interfaces:**
- Consumes: Secret `immich-postgres`, Service `immich-postgres`, PVC `immich-pgdump`.
- Produces: dated dumps at `/dumps/immich-YYYYMMDD-HHMMSS.sql.gz`, retained 14 days.

This is the only real recovery path for the database, since the spec deliberately excludes the Postgres data directory from Velero.

- [ ] **Step 1: Create `immich/pgdump-cronjob.yml`**

```yaml
---
apiVersion: batch/v1
kind: CronJob
metadata:
  name: immich-pgdump
  namespace: immich
spec:
  schedule: "0 4 * * *"
  concurrencyPolicy: Forbid
  successfulJobsHistoryLimit: 3
  failedJobsHistoryLimit: 3
  jobTemplate:
    spec:
      backoffLimit: 2
      template:
        spec:
          restartPolicy: OnFailure
          dnsConfig:
            options:
              - name: ndots
                value: "1"
          containers:
            - name: pgdump
              image: ghcr.io/immich-app/postgres:17-vectorchord1.1.1
              imagePullPolicy: IfNotPresent
              env:
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
                # MUST be /bin/bash, NOT /bin/sh. This image's /bin/sh is dash,
                # which has no `pipefail`; because `set` is a special builtin the
                # failure ABORTS the script on line 1 and the job can never
                # produce a backup. pipefail is load-bearing here: without it
                # `pg_dump | gzip > file` returns gzip's status, so a failed dump
                # exits 0 and writes a valid ~20-byte gzip. Do not "simplify".
                - /bin/bash
                - -c
                - |
                  set -euo pipefail
                  STAMP=$(date +%Y%m%d-%H%M%S)
                  OUT="/dumps/immich-${STAMP}.sql.gz"
                  echo "dumping to ${OUT}"
                  pg_dump --clean --if-exists | gzip -c > "${OUT}"
                  # Fail loudly if the dump is suspiciously small.
                  SIZE=$(stat -c %s "${OUT}")
                  echo "dump size: ${SIZE} bytes"
                  if [ "${SIZE}" -lt 10240 ]; then
                    echo "ERROR: dump smaller than 10KiB, treating as failure" >&2
                    exit 1
                  fi
                  find /dumps -name 'immich-*.sql.gz' -mtime +14 -delete
                  echo "done"
              volumeMounts:
                - name: dumps
                  mountPath: /dumps
              resources:
                requests:
                  cpu: 100m
                  memory: 256Mi
                limits:
                  cpu: "1"
                  memory: 1Gi
          volumes:
            - name: dumps
              persistentVolumeClaim:
                claimName: immich-pgdump
```

The size check exists because a `pg_dump` that silently produces an empty file is the classic way to discover you had no backup.

- [ ] **Step 2: Apply**

```bash
kubectl --context local-k3s apply -f immich/pgdump-cronjob.yml
```

- [ ] **Step 3: Trigger a run immediately rather than waiting for 04:00**

```bash
kubectl --context local-k3s create job -n immich --from=cronjob/immich-pgdump pgdump-manual-1
kubectl --context local-k3s wait --for=condition=complete job/pgdump-manual-1 -n immich --timeout=600s
```

Expected: condition met.

- [ ] **Step 4: Verify the dump exists and is non-trivial**

```bash
kubectl --context local-k3s logs -n immich job/pgdump-manual-1
```

Expected: a `dump size:` line well above 10240 bytes, followed by `done`.

- [ ] **Step 5: Verify the dump is actually readable**

A dump you have never decompressed is a dump you have never tested.

```bash
kubectl --context local-k3s run dumpcheck -n immich --rm -i --restart=Never \
  --image=ghcr.io/immich-app/postgres:17-vectorchord1.1.1 \
  --overrides='{"spec":{"containers":[{"name":"dumpcheck","image":"ghcr.io/immich-app/postgres:17-vectorchord1.1.1","command":["sh","-c","ls -la /dumps/ && gzip -t /dumps/*.sql.gz && echo GZIP_OK && zcat /dumps/*.sql.gz | head -20"],"volumeMounts":[{"name":"d","mountPath":"/dumps"}]}],"volumes":[{"name":"d","persistentVolumeClaim":{"claimName":"immich-pgdump"}}]}}'
```

Expected: `GZIP_OK` followed by PostgreSQL dump header lines.

- [ ] **Step 6: Clean up the manual job and commit**

```bash
kubectl --context local-k3s delete job pgdump-manual-1 -n immich
git add immich/pgdump-cronjob.yml
git commit -m "add immich postgres dump cronjob"
```

---

## Task 7: Monitoring alerts

**Files:**
- Create: `immich/alerts.yml`

**Interfaces:**
- Consumes: ServiceMonitors from `immich.metrics.enabled: true` (Task 4); the `immich-pgdump` CronJob (Task 6).
- Produces: PrometheusRule `immich-alerts` in the `monitoring` namespace.

The Prometheus instance has empty `serviceMonitorSelector`, `serviceMonitorNamespaceSelector`, `ruleSelector`, and `ruleNamespaceSelector`, so discovery is automatic. The labels below match the `velero/velero-alerts.yml` convention.

- [ ] **Step 1: Confirm Immich metrics are being scraped**

Do this before writing alerts — an alert on a metric that does not exist is worse than no alert.

```bash
kubectl --context local-k3s get servicemonitor -n immich
```

Expected: at least one ServiceMonitor.

```bash
kubectl --context local-k3s exec -n monitoring \
  prometheus-kube-prometheus-stack-prometheus-0 -c prometheus -- \
  wget -qO- 'http://localhost:9090/api/v1/query?query=up{namespace="immich"}'
```

Expected: JSON with one or more results where `"value"` ends in `"1"`. If the result list is empty, wait 60 seconds for a scrape cycle and retry before debugging.

- [ ] **Step 2: Create `immich/alerts.yml`**

```yaml
---
apiVersion: monitoring.coreos.com/v1
kind: PrometheusRule
metadata:
  name: immich-alerts
  namespace: monitoring
  labels:
    prometheus: kube-prometheus-stack
    role: alert-rules
spec:
  groups:
    - name: immich
      rules:
        - alert: ImmichServerDown
          expr: up{namespace="immich", job=~".*server.*"} == 0
          for: 10m
          labels:
            severity: critical
          annotations:
            summary: "Immich server is down"
            description: "The Immich server has been unreachable for 10 minutes. Check `kubectl -n immich get pods`."

        - alert: ImmichPostgresDown
          expr: kube_statefulset_status_replicas_ready{namespace="immich", statefulset="immich-postgres"} < 1
          for: 10m
          labels:
            severity: critical
          annotations:
            summary: "Immich Postgres has no ready replica"
            description: "immich-postgres has had no ready replica for 10 minutes. Immich cannot function without it."

        - alert: ImmichPgDumpFailed
          expr: |
            time() - kube_cronjob_status_last_successful_time{namespace="immich", cronjob="immich-pgdump"} > 172800
          for: 1h
          labels:
            severity: warning
          annotations:
            summary: "Immich pg_dump has not succeeded in 48h"
            description: "The nightly database dump is the only recovery path for Immich's database. Check `kubectl -n immich get jobs`."

        - alert: ImmichLibraryVolumeFilling
          expr: |
            (
              kubelet_volume_stats_used_bytes{namespace="immich", persistentvolumeclaim="immich-library"}
              / kubelet_volume_stats_capacity_bytes{namespace="immich", persistentvolumeclaim="immich-library"}
            ) > 0.85
          for: 30m
          labels:
            severity: warning
          annotations:
            summary: "Immich library volume above 85%"
            description: "The bulk pool backing the Immich library is filling up. It is a single 8TB disk with no redundancy."
```

- [ ] **Step 3: Apply**

```bash
kubectl --context local-k3s apply -f immich/alerts.yml
```

- [ ] **Step 4: Verify Prometheus loaded the rules**

```bash
kubectl --context local-k3s exec -n monitoring \
  prometheus-kube-prometheus-stack-prometheus-0 -c prometheus -- \
  wget -qO- 'http://localhost:9090/api/v1/rules' | grep -o 'ImmichServerDown\|ImmichPostgresDown\|ImmichPgDumpFailed\|ImmichLibraryVolumeFilling' | sort -u
```

Expected: all four alert names listed. Rule loading can take up to a minute after apply.

- [ ] **Step 5: Verify no alert is firing spuriously**

```bash
kubectl --context local-k3s exec -n monitoring \
  prometheus-kube-prometheus-stack-prometheus-0 -c prometheus -- \
  wget -qO- 'http://localhost:9090/api/v1/alerts' | grep -o '"alertname":"Immich[^"]*"' | sort -u
```

Expected: empty, or only alerts in `pending` that resolve. `ImmichPgDumpFailed` should not fire because Task 6 produced a successful run.

- [ ] **Step 6: Commit**

```bash
git add immich/alerts.yml
git commit -m "add immich alerts"
```

---

## Task 8: Velero backup integration

**Files:**
- Modify: `velero/values.yml`

**Interfaces:**
- Consumes: the exclusion annotations already placed on the Postgres, server, and machine-learning pods in Tasks 3 and 4.
- Produces: `immich` included in the `velero-homelab-daily` schedule.

- [ ] **Step 1: Verify the exclusion annotations are actually on the running pods**

Do this first. Adding the namespace to the schedule before the exclusions are live risks a nightly backup attempting to upload the entire library at ~2 MB/s.

```bash
kubectl --context local-k3s get pods -n immich -o json | python3 -c "
import json,sys
for p in json.load(sys.stdin)['items']:
    a = p['metadata'].get('annotations') or {}
    print(p['metadata']['name'], '->', a.get('backup.velero.io/backup-volumes-excludes', 'NONE'))
"
```

Expected:
- `immich-postgres-0` → `data`
- `immich-server-*` → `data`
- `immich-machine-learning-*` → `cache`
- `immich-valkey-*` → `NONE` (intentional; it is small and worth backing up)

If the server or machine-learning pods show `NONE`, the `controllers.main.pod.annotations` key in `immich/values.yml` did not take effect. Fix it and `helm upgrade` before continuing.

- [ ] **Step 2: Modify `velero/values.yml`**

Change the `includedNamespaces` list under `schedules.homelab-daily.template` from:

```yaml
      includedNamespaces:
        - home-assistant
        - dev-box
        - openclaw
        - openclaw-dottie
        - openclaw-stonk
```

to:

```yaml
      includedNamespaces:
        - home-assistant
        - dev-box
        - openclaw
        - openclaw-dottie
        - openclaw-stonk
        - immich
```

- [ ] **Step 3: Apply via helm upgrade**

```bash
helm --kube-context local-k3s upgrade velero vmware-tanzu/velero \
  --version 12.1.0 -n velero -f velero/values.yml \
  --set-file credentials.secretContents.cloud=./velero/credentials-velero
```

Note: `velero/credentials-velero` is git-ignored and must exist locally. If it does not, retrieve it from the existing secret rather than regenerating AWS keys:

```bash
kubectl --context local-k3s get secret velero -n velero -o jsonpath='{.data.cloud}' | base64 -d
```

- [ ] **Step 4: Verify the schedule now includes immich**

```bash
kubectl --context local-k3s get schedule velero-homelab-daily -n velero \
  -o jsonpath='{.spec.template.includedNamespaces}'
```

Expected: a list containing `immich`.

- [ ] **Step 5: Run an on-demand backup and confirm the exclusions held**

```bash
velero backup create immich-test --include-namespaces immich --wait
velero backup describe immich-test --details
```

Expected: backup `Completed`. In the details, confirm that **no** `PodVolumeBackup` exists for the `data` volume of `immich-server` or `immich-postgres-0`, nor for `cache` on machine-learning.

```bash
kubectl --context local-k3s get podvolumebackups -n velero \
  -l velero.io/backup-name=immich-test \
  -o custom-columns='POD:.spec.pod.name,VOLUME:.spec.volume,SIZE:.status.progress.totalBytes'
```

Expected: only the valkey data volume and the pgdump volume appear. If a multi-gigabyte `data` volume shows up, **cancel the backup immediately** (`velero backup delete immich-test`) and fix the annotations.

- [ ] **Step 6: Clean up and commit**

```bash
velero backup delete immich-test --confirm
git add velero/values.yml
git commit -m "add immich to velero backup schedule"
```

---

## Task 9: README runbook

**Files:**
- Create: `immich/README.md`

**Interfaces:**
- Consumes: everything from Tasks 1–8.
- Produces: the operational document, matching the `velero/README.md` and `home-assistant/README.md` style.

- [ ] **Step 1: Write `immich/README.md`**

It must contain, at minimum:

1. **What this is** — one paragraph: Immich, Tailscale-only at `immich.rya-scala.ts.net:2283`, library on `nfs-bulk`, Postgres as a hand-rolled StatefulSet.
2. **Why not CloudNativePG** — two sentences: k8s 1.29 is outside CNPG's supported range, and the chart's declarative-extensions example needs k8s ≥1.33 plus containerd ≥2.1.0. Revisit after the k3s upgrade. Without this note, a future reader will "fix" it by adding CNPG.
3. **Install order** — the exact commands from Tasks 1, 3, 4, 6, 7, in order.
4. **The `DB_STORAGE_TYPE` decision** from Task 2 and the reasoning.
5. **Retrieving the Postgres password:**
   ```bash
   kubectl --context local-k3s get secret immich-postgres -n immich \
     -o jsonpath='{.data.POSTGRES_PASSWORD}' | base64 -d
   ```
6. **Database restore procedure:**
   ```bash
   # List available dumps
   kubectl --context local-k3s exec -n immich immich-postgres-0 -- ls -la /dumps
   # Restore (scale Immich down first so it is not writing)
   kubectl --context local-k3s scale deploy immich-server -n immich --replicas=0
   # ... zcat the dump into psql ...
   kubectl --context local-k3s scale deploy immich-server -n immich --replicas=1
   ```
7. **The ML-settings gotcha** — because `IMMICH_CONFIG_FILE` is set, machine-learning and storage-template settings are **read-only in the admin UI**. Change them in `values.yml` and `helm upgrade`. This surprises people.
8. **Import procedure** — pointer to `import/` and Tasks 10–11.
9. **Backup posture** — library is *not* backed up; Google Photos is the source of truth. Postgres is protected by `pg_dump`, not Velero, and why.

- [ ] **Step 2: Verify the documented install commands are accurate**

Re-read the README against the actual files. Every command must name a file that exists and use `--context local-k3s`.

```bash
grep -n "kubectl\|helm" immich/README.md | grep -v "context local-k3s" || echo "all commands scoped to local-k3s"
```

Expected: `all commands scoped to local-k3s`, or only intentional exceptions.

- [ ] **Step 3: Commit**

```bash
git add immich/README.md
git commit -m "add immich readme"
```

---

## Task 10: Takeout ingest to the NAS

**Files:**
- Create: `immich/import/takeout-pvc.yml`

**Interfaces:**
- Consumes: `nfs-bulk` storage class; an NFS mount of the bulk pool on the workstation.
- Produces: PVC `immich-takeout` populated with integrity-verified Takeout archives.

> **GATE:** This task cannot start until the Google Takeout export is complete and its **download links are live**. Google delivers Takeout as browser-authenticated download links that expire roughly **seven days** after generation, with a limited number of retries per link. There is no server-side pull path: the links require the browser session, so `rclone`, `curl`, and in-cluster Jobs cannot fetch them. The export is therefore downloaded **by the browser on the workstation, writing directly onto the NAS over NFS**. Budget the download inside the seven-day window; if a link expires, regenerate that chunk from the Takeout page.

> **Why not rclone/Drive:** an earlier revision of this plan pulled the export from Google Drive with an `rclone` Job and an `immich-rclone` Secret. That design is dead — the export was delivered as expiring download links, not as Drive files. Do not create `immich/import/rclone-job.yml`, do not create the `immich-rclone` Secret, and do not configure a Drive remote. Nothing in Tasks 10–12 depends on them.

- [ ] **Step 1: Confirm the export is complete and note its true size**

On the Takeout page, record the **total size** and the **exact chunk count**. Write both down — Step 7 compares against them, and a missing chunk is otherwise invisible.

If the total exceeds 1.4 TB, raise the PVC size in Step 3 before applying. Also confirm the bulk pool has room — after Step 2 is done, this is simply:

```bash
df -h /mnt/takeout
```

Expected: free space comfortably exceeding the Takeout total (the library PVC shares this pool).

Note the chunk archive format while you are there — Takeout emits either `.zip` or `.tgz` depending on what was selected at export time. Step 8 handles both, but you should know which you have.

- [ ] **Step 2: Mount the bulk pool on the workstation**

The browser writes to the NAS directly; no local disk staging, and no second copy.

```bash
# Arch: nfs-utils provides mount.nfs. Debian/Ubuntu: nfs-common.
pacman -Qi nfs-utils >/dev/null 2>&1 || sudo pacman -S --needed nfs-utils

sudo mkdir -p /mnt/takeout
sudo mount -t nfs -o vers=4 192.168.5.1:/bulk-pool/k3s-bulk /mnt/takeout
mount | grep /mnt/takeout
```

Expected: a line reading `192.168.5.1:/bulk-pool/k3s-bulk on /mnt/takeout type nfs4 (rw,...)`.

The export `/bulk-pool/k3s-bulk` is shared to `192.168.0.0/16` over both NFSv3 and NFSv4; the workstation must be inside that range. It is an unauthenticated `sec=sys` export — the server trusts the client's uid, which is exactly what makes Step 5 work.

Do **not** unmount until the download and Step 7 are finished.

- [ ] **Step 3: Create `immich/import/takeout-pvc.yml`**

```yaml
---
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: immich-takeout
  namespace: immich
spec:
  storageClassName: nfs-bulk
  accessModes:
    - ReadWriteMany
  resources:
    requests:
      storage: 1500Gi
```

Apply it:

```bash
kubectl --context local-k3s apply -f immich/import/takeout-pvc.yml
kubectl --context local-k3s get pvc -n immich immich-takeout
```

Expected: `STATUS  Bound`, `CAPACITY  1500Gi`, `STORAGECLASS  nfs-bulk`. `nfs-bulk` is `Immediate`, so it binds without a consumer pod.

- [ ] **Step 4: Locate the provisioner-created directory**

`nfs-subdir-external-provisioner` names the directory `<namespace>-<pvc>-<pv-name>`. Derive it rather than typing it — the PV uid is random:

```bash
PV=$(kubectl --context local-k3s get pvc -n immich immich-takeout -o jsonpath='{.spec.volumeName}')
TAKEOUT_DIR="/mnt/takeout/immich-immich-takeout-${PV}"
echo "$TAKEOUT_DIR"
ls -ld "$TAKEOUT_DIR"
```

Expected: a path of the form `/mnt/takeout/immich-immich-takeout-pvc-<uuid>`, listed as `drwxrwxrwx 2 root root`.

The mode matters. The export root `/mnt/takeout` itself is `drwxr-xr-x root root`, so an unprivileged user cannot create anything at its top level. The provisioner creates PVC subdirectories world-writable, and that is the only reason the browser can write here. Do not attempt to `mkdir` in `/mnt/takeout` directly.

- [ ] **Step 5: Prove an unprivileged write works — before downloading anything**

The browser runs as your normal user, not root. Verify that uid actually has write access. **Do not use `sudo`** — sudo passing is not evidence.

```bash
echo "canary $(date -Is)" > "$TAKEOUT_DIR/.canary-write-test"
stat -c '%n uid=%u gid=%g mode=%a size=%s' "$TAKEOUT_DIR/.canary-write-test"
rm -f "$TAKEOUT_DIR/.canary-write-test"
```

Expected: the `stat` line reports `uid=<your uid>` (e.g. `uid=1000`) and a non-zero size, and the `rm` succeeds.

If this fails with `Permission denied`, **stop**. Every later step depends on it. Check that the mount is `rw` and not `ro`, that the export is not squashing your uid to `nobody`, and that the directory really is mode `777`. Do not work around it by running the browser as root.

- [ ] **Step 6: Point the browser at the directory and download every chunk**

Set the browser's download directory to the **exact** path printed in Step 4:

- Firefox: Settings → General → Downloads → *Save files to* → the `$TAKEOUT_DIR` path. Also **uncheck** *Always ask you where to save files*, otherwise every chunk needs a manual dialog.
- Chrome/Chromium: Settings → Downloads → *Location* → the `$TAKEOUT_DIR` path, and turn *Ask where to save each file* off.

Then open the Takeout page and click every chunk link. Browsers queue downloads rather than running them all at once, so clicking through all of them in one pass is fine and is the intended workflow — do not hand-download one per day, the links expire in about seven days.

Notes:
- Leave the machine awake and the mount up. A suspend that drops the NFS mount mid-write corrupts the in-flight chunk (caught in Step 8, but it costs a re-download).
- Throughput is bounded by the LAN and the NAS, not by Google.
- Do not rename the files. Task 11 globs on the archive extension.

- [ ] **Step 7: Verify the file count and total size, and that nothing is still in flight**

```bash
ls -la "$TAKEOUT_DIR"
ls -1 "$TAKEOUT_DIR" | wc -l
du -sh "$TAKEOUT_DIR"
find "$TAKEOUT_DIR" \( -name '*.crdownload' -o -name '*.part' -o -name '*.partial' \) -print
```

Expected: the file count equals the chunk count from Step 1, `du -sh` matches the Takeout total, and the `find` prints **nothing**. Any `.crdownload`/`.part` file is an unfinished or abandoned download — delete it and re-fetch that chunk.

A count or size that is short means a link expired or a click was missed. Fix it now; a missing chunk is silently missing photos.

- [ ] **Step 8: Verify the archives are not corrupt**

This runs in-cluster against the PVC, not on the workstation — it reads every byte of every archive and the NAS is the right side of that transfer. It handles `.zip` and `.tgz`/`.tar.gz`, so it is correct whichever format Takeout produced.

`unzip` here is Info-ZIP from Debian, not busybox: Takeout chunks above 4 GB are zip64, which busybox `unzip` cannot read, and a false `CORRUPT` on every file is worse than no check at all.

```bash
kubectl --context local-k3s apply -f - <<'EOF'
---
apiVersion: batch/v1
kind: Job
metadata:
  name: immich-takeout-verify
  namespace: immich
spec:
  backoffLimit: 0
  template:
    metadata:
      annotations:
        backup.velero.io/backup-volumes-excludes: takeout
    spec:
      restartPolicy: Never
      nodeSelector:
        kubernetes.io/hostname: k3s-node-1
      dnsConfig:
        options:
          - name: ndots
            value: "1"
      containers:
        - name: verify
          image: debian:bookworm-slim
          command:
            - /bin/sh
            - -c
            - |
              set -eu
              apt-get update -qq
              apt-get install -y -qq --no-install-recommends unzip >/dev/null
              cd /takeout
              found=0
              fail=0
              for f in *; do
                [ -f "$f" ] || continue
                case "$f" in
                  *.zip)
                    found=$((found + 1))
                    if unzip -t -qq "$f" >/dev/null 2>&1; then
                      echo "OK       $f"
                    else
                      echo "CORRUPT  $f"
                      fail=$((fail + 1))
                    fi
                    ;;
                  *.tgz|*.tar.gz)
                    found=$((found + 1))
                    if tar -tzf "$f" >/dev/null 2>&1; then
                      echo "OK       $f"
                    else
                      echo "CORRUPT  $f"
                      fail=$((fail + 1))
                    fi
                    ;;
                  *)
                    echo "SKIPPED  $f (not .zip/.tgz/.tar.gz)"
                    ;;
                esac
              done
              echo "--- checked $found archives, $fail corrupt ---"
              if [ "$found" -eq 0 ]; then
                echo "ERROR: no archives found in /takeout"
                exit 1
              fi
              [ "$fail" -eq 0 ]
          volumeMounts:
            - name: takeout
              mountPath: /takeout
              readOnly: true
          resources:
            requests:
              cpu: 500m
              memory: 256Mi
            limits:
              cpu: "2"
              memory: 1Gi
      volumes:
        - name: takeout
          persistentVolumeClaim:
            claimName: immich-takeout
EOF

kubectl --context local-k3s logs -n immich -f job/immich-takeout-verify
kubectl --context local-k3s wait --for=condition=complete job/immich-takeout-verify -n immich --timeout=43200s
```

This is a full read of the entire export over NFS — hours, not minutes.

The `backup.velero.io/backup-volumes-excludes: takeout` annotation is mandatory, not decorative. The `immich` namespace **is** in the Velero backup schedule (Task 8), so any pod mounting the 1.5 Ti takeout volume without that annotation would have its next nightly backup try to upload the whole export at roughly 2 MB/s. Every pod that mounts `immich-takeout` — this Job and the Task 11 import Job — must carry it.

Expected: one `OK` line per chunk, a trailing `--- checked <N> archives, 0 corrupt ---` where `<N>` equals the Step 1 chunk count, and the Job reaching `complete`.

Any `CORRUPT` line means a truncated or damaged chunk, which silently skips photos on import. Re-download that chunk from Takeout and re-run the Job. Any `SKIPPED` line means a stray file — investigate it before continuing. The Job exiting non-zero is the intended failure signal; do not start Task 11 until it passes.

Clean up the Job once it is green:

```bash
kubectl --context local-k3s delete job immich-takeout-verify -n immich
```

- [ ] **Step 9: Commit**

```bash
git add immich/import/takeout-pvc.yml
git commit -m "add immich takeout pvc"
```

---

## Task 11: Import with immich-go (two passes)

**Files:**
- Create: `immich/import/immich-go-job.yml` (contains **both** Jobs)

**Interfaces:**
- Consumes: PVC `immich-takeout` (Task 10); Secret `immich-api` (Task 5); the in-cluster server Service.
- Produces: the imported library, with dates, albums, and GPS preserved.

The export did not land as a single clean set of zips. It must be imported in **two passes**:

| Pass | Source | Mode |
| --- | --- | --- |
| 1 | the `takeout-*.zip` chunks | `upload from-google-photos` over the zips |
| 2 | one standalone `PXL_20250517_140916476-036.mp4` (19.5 GB) | `upload from-google-photos` over a small staged folder |

Google served that MP4 **outside** the zips because it exceeds the 10 GB split size, so it occupies chunk slot `036` and no `036.zip` exists. A `*.zip` glob cannot see it. Its sidecar **is** inside the zips — verified present at `Takeout/Google Photos/Photos from 2025/PXL_20250517_140916476.mp4.supplemental-metadata.json` in `takeout-20260913T100905Z-1-035.zip`. Pass 2 re-pairs the two.

Do not fall back to filename-date parsing for that video. Its sidecar records `photoTakenTime` `1747493384` = 2025-05-17 **14:49:44 UTC** plus GPS `40.0398, -75.2457`, whereas the filename encodes `14:09:16`. The filename is both ~40 minutes off and timezone-ambiguous, and carries no GPS.

> **Note on distribution:** immich-go publishes **release binaries only — there is no official container image.** Any plan or snippet referencing `ghcr.io/simulot/immich-go` is wrong. The Jobs download and checksum-verify the official tarball into a stock Debian image.
>
> Pinned for v0.32.0, `linux/amd64` (all four nodes are amd64):
> - URL: `https://github.com/simulot/immich-go/releases/download/v0.32.0/immich-go_Linux_x86_64.tar.gz`
> - SHA256: `6e2ad86bafdadb9466d6515de7cb882726c0aea1a21d51164dff361d7d480a97`

- [ ] **Step 0: Confirm the download is actually finished**

**The chunk sequence runs past 050.** Chunk `054` exists, so the set is `001`–`054` with `036` being the standalone MP4 — i.e. **53 zips + 1 MP4**, not the 49 zips assumed earlier. Any Chrome `*.crdownload` file in the directory is a download still in flight.

```bash
D=/mnt/takeout/immich-immich-takeout-pvc-e995c66a-e1ed-444f-a66b-51227a1db4c9
ls "$D"/*.crdownload 2>/dev/null && echo "DOWNLOADS STILL RUNNING - STOP"
ls -1 "$D"/takeout-*.zip | sed -n 's/.*-1-\([0-9]\{3\}\)\.zip$/\1/p' | sort -n | uniq
```

The `sed -n .../p` form is deliberate: it matches only names ending in exactly `NNN.zip`, so the duplicate `049 (1).zip` is excluded from the sequence check instead of corrupting it.

Expected: no `.crdownload` files, and a contiguous `001..054` sequence with `036` absent — 53 entries. A gap means a chunk never downloaded and those photos would be silently missing. Do not start Pass 1 until this passes.

**Status at time of writing: this check passes.** All 53 zips are present (`001`–`054`, no `036`), no `.crdownload` files remain, and the standalone `PXL_20250517_140916476-036.mp4` is present at 19,511,783,135 bytes. `054` is the highest chunk observed; it is treated as the end of sequence because the numbering is contiguous up to it and Chrome has no downloads in flight.

- [ ] **Step 0b: Resolve the duplicate chunk 049**

`takeout-20260913T100905Z-1-049 (1).zip` is a second download of chunk 049. Left in place it is matched by the `*.zip` glob and chunk 049 is fed in twice.

**Integrity-check both before removing either** — if they differ, one is corrupt and you need to keep the good one.

```bash
D=/mnt/takeout/immich-immich-takeout-pvc-e995c66a-e1ed-444f-a66b-51227a1db4c9
sha256sum "$D/takeout-20260913T100905Z-1-049.zip" "$D/takeout-20260913T100905Z-1-049 (1).zip"
```

Expected: two identical digests. Only then remove `049 (1).zip`. If the digests differ, test both with `unzip -t` and keep the one that passes. (Their central directories have already been compared and match exactly — 8339 entries, identical names, sizes and CRCs — so identical digests are the expected outcome, but read 20 GB and confirm rather than assuming.)

- [ ] **Step 1: immich-go v0.32.0 syntax — verified**

Verified against the actual v0.32.0 binary (`commit f7d19fce`, `date 2026-06-25`). These are the real flag names, not assumptions:

| Purpose | Flag |
| --- | --- |
| Server URL | `-s`, `--server` |
| API key | `-k`, `--api-key` |
| Dry run | `--dry-run` |
| Media source | **positional**, after the flags |
| Suppress TUI | `--no-ui` (required for readable `kubectl logs`) |
| Error handling | `--on-errors` (`stop` default, `continue`, or a max count) |
| Parallelism | `--concurrent-tasks` |

Usage strings, verbatim:

```
immich-go upload from-google-photos [flags] <takeout-*.zip> | <takeout-folder>
immich-go upload from-folder        [flags] <path>...
```

So `from-google-photos` accepts **either** multiple zip paths (a shell glob expands correctly) **or** one decompressed takeout folder. That folder form is what Pass 2 uses.

Date-from-filename is **`--date-from-name`, and it exists only on `from-folder`** (default true, applies to jpg/mp4/heic/dng/cr2/cr3/arw/raf/nef/mov). `from-google-photos` has no such flag — it takes dates from the JSON sidecars. This is why Pass 2 uses `from-google-photos` on a staged folder rather than `from-folder`: `from-folder` would discard the sidecar's GPS and use the wrong timestamp.

Two gotchas in the help output:
- `--concurrent-tasks` documents its range as `1-20` but defaults to `24`. Always set it explicitly.
- `--pause-immich-jobs` defaults to **true**, which is what we want — it keeps thumbnail/ML jobs off the SMR library disk during upload.

- [ ] **Step 2: Create `immich/import/immich-go-job.yml`**

The file defines **two** Jobs, `immich-go-import-zips` (Pass 1) and `immich-go-import-standalone` (Pass 2). See the file in the repo for the full manifest.

Properties that are load-bearing in both Jobs — do not "clean them up":

- **`backup.velero.io/backup-volumes-excludes: takeout` on the pod template is mandatory.** The `immich` namespace **is** in the `velero-homelab-daily` schedule (verified: its `includedNamespaces` lists `immich`). Without this annotation the next nightly backup attempts to upload the whole ~1.5 Ti takeout volume at roughly 2 MB/s. The annotation value must match the volume name `takeout`.
- **`restartPolicy: Never` + `backoffLimit: 0`, and no `activeDeadlineSeconds`.** The import runs for hours; an auto-retry restarts from scratch and burns all of them. Failures are investigated by hand.
- **Target the in-cluster Service** `http://immich-server.immich.svc.cluster.local:2283`, never the tailnet hostname — there is no reason to push hundreds of gigabytes through the Tailscale proxy.
- **`--no-ui`**, or immich-go renders a TUI into the pod log and `kubectl logs` becomes unreadable.
- **`--concurrent-tasks=4`** in Pass 1 (`=1` in Pass 2). The default of `24` is outside the tool's own documented `1-20` range, and high write concurrency is actively harmful on the SMR library disk.
- Pass 1 mounts `/takeout` **read-only** and aborts if `049 (1).zip` is still present. Pass 2 mounts it **read-write** because it stages the sidecar and a hardlink.

Pass 2 stages a minimal decompressed-takeout tree so immich-go sees a media + sidecar pair:

```
/takeout/_standalone036/Takeout/Google Photos/Photos from 2025/
    PXL_20250517_140916476.mp4                              <- hardlink to the 19.5 GB file
    PXL_20250517_140916476.mp4.supplemental-metadata.json   <- unzipped from chunk 035
```

The staged media file must use the **original** name with no `-036` chunk suffix, because the sidecar's `"title"` is `PXL_20250517_140916476.mp4` and that is how immich-go pairs them. The link is a **hardlink**, not a copy: same NFS filesystem, so no 19.5 GB of data moves. The Job falls back to `cp` if the NFS server refuses the link.

Apply one Job at a time — never the whole file:

```bash
# usage: jobsel <job-name> [dryrun]
jobsel() {
  python3 -c "
import sys,yaml
name,dry=sys.argv[1],len(sys.argv)>2
for d in yaml.safe_load_all(open('immich/import/immich-go-job.yml')):
    if d and d['metadata']['name']==name:
        if dry:
            d['metadata']['name']=name+'-dryrun'
            d['spec']['template']['spec']['containers'][0]['env'].append({'name':'DRY_RUN','value':'1'})
        print(yaml.safe_dump(d))
" "$@"
}
```

- [ ] **Step 3: Pass 1 dry run**

```bash
jobsel immich-go-import-zips dryrun | kubectl --context local-k3s apply -f -
kubectl --context local-k3s logs -n immich -f job/immich-go-import-zips-dryrun
```

The effective command line inside the pod is:

```
immich-go upload from-google-photos \
  --server=http://immich-server.immich.svc.cluster.local:2283 \
  --api-key=$IMMICH_API_KEY --no-ui --concurrent-tasks=4 \
  --on-errors=continue --dry-run /takeout/*.zip
```

Expected: the printed chunk count matches Step 0, and a plausible asset total with no auth or parse errors. **Chunk 035 will report an unmatched JSON** for `PXL_20250517_140916476.mp4` — that is correct and expected, the media for it arrives in Pass 2.

- [ ] **Step 4: Pass 1 real run**

```bash
kubectl --context local-k3s delete job immich-go-import-zips-dryrun -n immich
jobsel immich-go-import-zips | kubectl --context local-k3s apply -f -
kubectl --context local-k3s logs -n immich -f job/immich-go-import-zips
kubectl --context local-k3s wait --for=condition=complete job/immich-go-import-zips -n immich --timeout=172800s
```

Runs for many hours. No `activeDeadlineSeconds`, so it will not be killed mid-flight.

- [ ] **Step 5: Pass 2 — the standalone MP4**

```bash
jobsel immich-go-import-standalone dryrun | kubectl --context local-k3s apply -f -
kubectl --context local-k3s logs -n immich -f job/immich-go-import-standalone-dryrun
```

The effective command line is the same tool and mode, pointed at the staged folder:

```
immich-go upload from-google-photos \
  --server=http://immich-server.immich.svc.cluster.local:2283 \
  --api-key=$IMMICH_API_KEY --no-ui --concurrent-tasks=1 \
  --dry-run /takeout/_standalone036
```

Expected: **exactly one** asset discovered, dated 2025-05-17, with GPS. If the dry run reports zero assets, immich-go did not accept the staged tree as a takeout folder — then, and only then, fall back to `upload from-folder --date-from-name /takeout/_standalone036/...`, accepting that GPS is lost and the timestamp comes from the filename (~40 minutes off). Do not reach for the fallback before the dry run proves it is needed.

Then the real run:

```bash
kubectl --context local-k3s delete job immich-go-import-standalone-dryrun -n immich
jobsel immich-go-import-standalone | kubectl --context local-k3s apply -f -
kubectl --context local-k3s logs -n immich -f job/immich-go-import-standalone
kubectl --context local-k3s wait --for=condition=complete job/immich-go-import-standalone -n immich --timeout=86400s
```

Afterwards remove the staging tree (the hardlink, not the original):

```bash
rm -rf "$D/_standalone036"
```

- [ ] **Step 6: Verify the import completed and counts are sane**

```bash
kubectl --context local-k3s exec -n immich immich-postgres-0 -- \
  psql -U immich -d immich -c "SELECT count(*) FROM asset;"
kubectl --context local-k3s exec -n immich immich-postgres-0 -- \
  psql -U immich -d immich -c "SELECT count(*) FROM album;"
kubectl --context local-k3s exec -n immich immich-postgres-0 -- \
  psql -U immich -d immich -c "SELECT count(*) FROM \"user\";"
```

Table names are singular in v3.2.0 (`StandardizeNames` migration) — verified against the live database, which has `asset`, `album` and `user`. `assets`/`albums`/`users` **do not exist**. `user` is a reserved word and must be quoted.

Confirm the big video specifically landed, with real metadata rather than an import-time default:

```bash
kubectl --context local-k3s exec -n immich immich-postgres-0 -- psql -U immich -d immich -c \
  "SELECT \"originalFileName\", \"fileCreatedAt\" FROM asset WHERE \"originalFileName\" LIKE 'PXL_20250517_140916476%';"
```

Expected: the asset count is close to the Pass 1 dry-run figure plus one, the album count is non-zero — a zero album count means the `.json` sidecars were not parsed, which defeats the point of using immich-go — and the video's `fileCreatedAt` is 2025-05-17, not today.

- [ ] **Step 7: Spot-check metadata quality in the UI**

Open the web UI and confirm on a sample of photos:
- capture dates are historical, **not** the import date (the single most common Takeout import failure)
- albums from Google Photos are present
- GPS/map data appears for photos that had it
- the 40-minute 4K HEVC video from 2025-05-17 plays and sits on its correct date

If dates are all "today", stop — the sidecars were not read, and the fix is the immich-go invocation, not a re-import on top of bad data.

- [ ] **Step 8: Verify library files landed on the bulk pool**

```bash
kubectl --context local-k3s exec -n immich deploy/immich-server -- du -sh /data
```

The library mounts at `/data` in v3.2.0 — `/usr/src/app/upload` does not exist in this image.

Expected: a size on the order of the Takeout total.

- [ ] **Step 9: Commit**

```bash
git add immich/import/immich-go-job.yml
git commit -m "add immich-go import job"
```

### Known forward risks

Neither of these blocks the import, but both will shape what goes wrong during it. Watch for them rather than being surprised.

**1. valkey can OOM under deep BullMQ queues.** Verified on the live deployment: `immich-valkey` has a hard `limits.memory: 512Mi`, and `redis-cli config get` reports `maxmemory 0` with `maxmemory-policy noeviction`. valkey therefore has no idea it is capped — it will never evict, it will simply grow until the kernel OOM-kills the container. An import of this size pushes very deep metadata-extraction and thumbnail queues into Redis. If the valkey pod starts restarting mid-import, that is this, and in-flight queued jobs are lost (the uploaded assets survive in Postgres; the derived work has to be re-queued from the admin job panel). Mitigations if it bites: raise the limit, or set `maxmemory` to roughly 80% of the limit with `allkeys-lru`.

**2. The library sits on an SMR drive.** Shingled media rewrites whole bands for small random writes. Thumbnail generation is exactly that workload — millions of small files — so it will be slow, and it will stay slow long after the upload itself finishes. This is why `--pause-immich-jobs` (default true) matters, why ML stays disabled until Task 12, and why `--concurrent-tasks` is held at 4. Expect the post-import thumbnail backlog to take substantially longer than the transfer did; do not read a stalled-looking job queue as a failure before checking disk latency.

---

## Task 12: Enable machine learning and clean up

**Files:**
- Modify: `immich/values.yml`

**Interfaces:**
- Consumes: the completed import (Task 11).
- Produces: CLIP search and facial recognition enabled; transient resources removed.

Because `IMMICH_CONFIG_FILE` is set, these settings are **read-only in the admin UI**. They must be changed in `values.yml` and applied with `helm upgrade`. Attempting this in the UI will appear to do nothing.

- [ ] **Step 1: Modify `immich/values.yml`**

Under `immich.configuration.machineLearning`, change the flags to `true` and drop the now-stale comment. **Four** flags were disabled for the import, not two:

```yaml
    machineLearning:
      clip:
        enabled: true
      facialRecognition:
        enabled: true
      duplicateDetection:
        enabled: true
      ocr:
        enabled: false
```

`duplicateDetection` is cheap once CLIP embeddings exist — it reuses them — so re-enable it. **`ocr` is a deliberate decision, not an oversight:** it runs a separate CPU-only inference pass over every asset on a cluster with no GPU, and it is the one ML task whose output most people never search. Leave it `false` unless you specifically want text-in-image search, and if you do, enable it *after* Smart Search and Face Detection have drained.

Also consider restoring `immich.configuration.job.thumbnailGeneration.concurrency` — it is set to `1` for the SMR disk. Leave it at `1` unless thumbnailing proves to be the bottleneck; the drive, not the CPU, is the limit.

- [ ] **Step 2: Apply**

```bash
helm --kube-context local-k3s upgrade immich \
  oci://ghcr.io/immich-app/immich-charts/immich \
  --version 0.13.2 -n immich -f immich/values.yml
```

- [ ] **Step 3: Verify the ConfigMap actually changed**

```bash
kubectl --context local-k3s get cm immich-immich-config -n immich -o jsonpath='{.data.immich-config\.yaml}'
```

Expected: `clip.enabled`, `facialRecognition.enabled`, and `duplicateDetection.enabled` are `true`, and `ocr.enabled` is still `false`. The chart does not put a config checksum on the pod template, so `helm upgrade` alone will **not** restart the server — restart it explicitly and wait:

```bash
kubectl --context local-k3s rollout restart deploy/immich-server -n immich
kubectl --context local-k3s rollout status deploy/immich-server -n immich
```

Then confirm the running process actually picked it up, rather than trusting the ConfigMap:

```bash
kubectl --context local-k3s run immich-probe --rm -i --restart=Never -n immich --image=curlimages/curl:latest -- \
  curl -s http://immich-server.immich.svc.cluster.local:2283/api/server/features
```

Expected: `"smartSearch":true`, `"facialRecognition":true`, `"duplicateDetection":true`, `"ocr":false`. This endpoint is derived from the loaded config, so it reflects the process, not the file. Also check the logs for `Unknown keys found` — Immich warns rather than fails on a misspelled config key, so a typo is otherwise silent.

- [ ] **Step 4: Queue the ML jobs**

In the admin UI: Administration → Jobs. Run **Smart Search** and **Face Detection** with the "Missing" option.

- [ ] **Step 5: Confirm jobs are progressing**

```bash
kubectl --context local-k3s logs -n immich -l app.kubernetes.io/name=machine-learning --tail=30
```

Expected: inference activity. Expect **1–3 days** of CPU-only processing for a library this size. Monitor node pressure:

```bash
kubectl --context local-k3s top nodes
```

If node-1 or node-3 is saturated to the point of affecting other workloads, lower `immich.configuration.job.*.concurrency` in `values.yml` and re-apply, rather than killing the pods. Note this cannot be done in the admin UI: `IMMICH_CONFIG_FILE` is set, so job settings are read-only there too.

- [ ] **Step 6: Verify search works once jobs finish**

Search for a generic term such as "beach" or "dog" in the UI. Results prove the CLIP pipeline is functioning end to end.

```bash
kubectl --context local-k3s exec -n immich immich-postgres-0 -- \
  psql -U immich -d immich -c "SELECT count(*) FROM smart_search;"
```

Expected: a count approaching the total asset count.

- [ ] **Step 7: Remove the transient takeout volume**

Only after Steps 5 and 6 confirm a good import.

```bash
kubectl --context local-k3s delete job immich-go-import immich-takeout-verify -n immich --ignore-not-found
kubectl --context local-k3s delete -f immich/import/takeout-pvc.yml
```

`archiveOnDelete: "true"` means the NFS directory is archived rather than destroyed, so this is recoverable. Reclaim the space on the NAS manually once you are confident.

- [ ] **Step 8: Verify the bulk pool has room**

```bash
kubectl --context local-k3s exec -n immich deploy/immich-server -- df -h /data
```

Expected: usage consistent with the library alone, with the takeout no longer counted.

- [ ] **Step 9: Update the README and commit**

Add a dated note to `immich/README.md` in the `home-assistant/README.md` incident style, recording the import date, final asset count, and how long ML took — that last figure is the one you will want if you ever rebuild.

```bash
git add immich/values.yml immich/README.md
git commit -m "enable immich machine learning after import"
```

---

## Self-Review

**1. Spec coverage.** Every spec section maps to a task:

| Spec section | Task |
|---|---|
| Architecture (4 components) | 3, 4 |
| Storage (6 PVCs) | 1, 10 |
| Database (StatefulSet, node-3, DB_STORAGE_TYPE, vchord) | 2, 3 |
| DNS mitigations (FQDN + ndots) | 3, 4 |
| Access (Tailscale annotations) | 4, 5 |
| Resources and scheduling (node-2 exclusion) | 3, 4 |
| ML disabled during import | 4, 12 |
| Storage template before import | 4 |
| Import pipeline stages 1–4 | 10, 11, 12 |
| Backup (Velero exclusions, pg_dump) | 3, 4, 6, 8 |
| Monitoring | 7 |
| Repository layout | 1, 9 |
| Open items 1–3 | 2, 10 step 1, 11 step 1 |

No gaps.

**2. Placeholder scan.** One intentional placeholder remains: `__DB_STORAGE_TYPE__` in Task 3, which Task 2 exists specifically to resolve, and Task 3 Step 3 states the substitution explicitly. Two values require environment-specific input and say so at point of use: the provisioned takeout directory path, which is derived at runtime (Task 10 Step 4), and the API key (Task 5 Step 5). No `TBD`, `TODO`, or "handle errors appropriately" instructions.

**2a. External reference verification.** Every image and metric referenced was checked against its registry or against the live cluster, not recalled:

| Reference | Status |
|---|---|
| `oci://ghcr.io/immich-app/immich-charts/immich:0.13.2` | verified, appVersion v3.2.0 |
| `ghcr.io/immich-app/postgres:17-vectorchord1.1.1` | verified, HTTP 200 |
| `curlimages/curl:8.10.1`, `busybox:1.36`, `debian:bookworm-slim` | verified |
| `ghcr.io/simulot/immich-go` | **does not exist — corrected.** immich-go ships release binaries only; Task 11 now downloads and checksum-verifies the tarball |
| `prometheus-kube-prometheus-stack-prometheus-0` | verified, running |
| `kube_cronjob_status_last_successful_time`, `kube_statefulset_status_replicas_ready`, `kubelet_volume_stats_{used,capacity}_bytes` | all present; kube-state-metrics is deployed |
| Chart values keys (`server.service.main.annotations`, `defaultPodOptions.dnsConfig`, `immich.persistence.library.existingClaim`, `immich.configuration`) | verified by rendering chart 0.13.2 |
| `{{y}}` in values survives Helm without rendering | verified by rendering |

The immich-go image was a genuine error in the first draft of this plan — the kind that would have failed at `ImagePullBackOff` after the multi-day Takeout download had already completed.

**3. Name consistency.** Verified across tasks:
- PVCs: `immich-library`, `immich-ml-cache`, `immich-pgdump`, `immich-postgres-data`, `immich-valkey-data`, `immich-takeout` — consistent in Tasks 1, 3, 4, 6, 8, 10, 11, 12.
- Secrets: `immich-postgres` (3), `immich-api` (5, 11). Task 10 needs no secret — the Takeout download is browser-authenticated on the workstation.
- Services and ports: `immich-server:2283`, `immich-machine-learning:3003`, `immich-valkey:6379`, `immich-postgres:5432` — all verified against rendered chart output.
- Velero volume names in exclusion annotations (`data`, `cache`, `takeout`) match the volume names in the pod specs that define them, checked in Task 8 Step 1.
- ConfigMap `immich-immich-config` matches the rendered chart name.

**4. Ordering.** Tasks 1–9 are the steady-state deployment and can run back-to-back. Tasks 10–12 are gated on the Google Takeout export existing and are explicitly marked. Task 8 depends on annotations created in Tasks 3 and 4 and verifies them before acting, rather than assuming.
