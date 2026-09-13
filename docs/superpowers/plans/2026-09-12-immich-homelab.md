# Immich on local-k3s Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Deploy Immich to the `local-k3s` cluster with Tailscale-only access, then bulk-import a 300 GB–1 TB Google Photos library from Google Takeout.

**Architecture:** The official Immich Helm chart (OCI, 0.13.2) provides server, machine-learning, and valkey. PostgreSQL is a hand-written single-replica StatefulSet using Immich's own image, which ships VectorChord preinstalled — CloudNativePG is unusable on this cluster's EOL Kubernetes 1.29. The photo library lives on an NFS-backed `nfs-bulk` PVC; the database lives on node-local `local-path`. Access is a Tailscale L4 Service proxy, not an Ingress.

**Tech Stack:** k3s v1.29.4, Helm (OCI registry), Immich v3.2.0, PostgreSQL 17 + VectorChord 1.1.1, Valkey, nfs-subdir-external-provisioner, Tailscale operator, Velero, kube-prometheus-stack, immich-go v0.32.0, rclone.

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
    rclone-job.yml        Google Drive -> NAS
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

Expected: Immich's tables exist (`assets`, `users`, `albums`, and others).

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
                - /bin/sh
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
- Create: `immich/import/rclone-job.yml`

**Interfaces:**
- Consumes: `nfs-bulk` storage class.
- Produces: PVC `immich-takeout` populated with Takeout archives.

> **GATE:** This task cannot start until the Google Takeout export has been requested **with "Add to Drive" as the destination** and Google reports it complete. Do not use emailed download links — with 10 GB chunks this is 30–100 files, and the links expire in about seven days with limited retries.

- [ ] **Step 1: Confirm the export is complete and note its true size**

Check Google Takeout. Record the total size and file count. If the total exceeds 1.4 TB, increase the PVC size in Step 2 accordingly.

- [ ] **Step 2: Create `immich/import/takeout-pvc.yml`**

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

- [ ] **Step 3: Create the rclone config secret**

Generate an rclone remote for Google Drive locally (`rclone config`, remote named `gdrive`), then:

```bash
kubectl --context local-k3s create secret generic immich-rclone -n immich \
  --from-file=rclone.conf="$HOME/.config/rclone/rclone.conf"
```

The local `immich/rclone.conf` path is git-ignored by Task 1.

- [ ] **Step 4: Create `immich/import/rclone-job.yml`**

Adjust `gdrive:Takeout` to the actual Drive folder path.

```yaml
---
apiVersion: batch/v1
kind: Job
metadata:
  name: immich-rclone-takeout
  namespace: immich
spec:
  backoffLimit: 3
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
        - name: rclone
          image: rclone/rclone:1.68
          command:
            - /bin/sh
            - -c
            - |
              # NOTE: rclone/rclone is Alpine-based, so /bin/sh is busybox ash,
              # NOT bash. Do NOT add `pipefail` here — Task 6 proved that
              # `set -o pipefail` under a non-bash /bin/sh aborts the script on
              # line 1 as a special-builtin failure. There are no pipes in this
              # script, so `set -eu` is sufficient and correct.
              set -eu
              rclone --config /cfg/rclone.conf copy \
                gdrive:Takeout /takeout \
                --transfers 4 --checkers 8 \
                --progress --stats 1m --stats-one-line
              echo "--- transferred ---"
              ls -la /takeout
              du -sh /takeout
          volumeMounts:
            - name: cfg
              mountPath: /cfg
              readOnly: true
            - name: takeout
              mountPath: /takeout
          resources:
            requests:
              cpu: 500m
              memory: 512Mi
            limits:
              cpu: "2"
              memory: 2Gi
      volumes:
        - name: cfg
          secret:
            secretName: immich-rclone
        - name: takeout
          persistentVolumeClaim:
            claimName: immich-takeout
```

The Velero exclusion annotation is present from the start — without it, the first nightly backup after this Job starts would try to upload up to 1.5 TB at ~2 MB/s.

- [ ] **Step 5: Apply and monitor**

```bash
kubectl --context local-k3s apply -f immich/import/takeout-pvc.yml
kubectl --context local-k3s apply -f immich/import/rclone-job.yml
kubectl --context local-k3s logs -n immich -f job/immich-rclone-takeout
```

This runs for hours. Monitor the one-line stats.

- [ ] **Step 6: Verify the transfer is complete and sizes match**

```bash
kubectl --context local-k3s wait --for=condition=complete job/immich-rclone-takeout -n immich --timeout=86400s
kubectl --context local-k3s logs -n immich job/immich-rclone-takeout --tail=30
```

Expected: `du -sh /takeout` reports a size matching the Takeout total from Step 1, and the file count matches. A short transfer means silent failure — re-run rclone, which resumes.

- [ ] **Step 7: Verify the archives are not corrupt**

```bash
kubectl --context local-k3s run zipcheck -n immich --rm -i --restart=Never \
  --image=busybox:1.36 \
  --overrides='{"spec":{"containers":[{"name":"zipcheck","image":"busybox:1.36","command":["sh","-c","for f in /takeout/*.zip; do unzip -t \"$f\" >/dev/null 2>&1 && echo \"OK $f\" || echo \"CORRUPT $f\"; done"],"volumeMounts":[{"name":"t","mountPath":"/takeout"}]}],"volumes":[{"name":"t","persistentVolumeClaim":{"claimName":"immich-takeout"}}]}}'
```

Expected: every file reports `OK`. Re-download any `CORRUPT` file before importing — a truncated archive silently skips photos.

- [ ] **Step 8: Commit**

```bash
git add immich/import/takeout-pvc.yml immich/import/rclone-job.yml
git commit -m "add immich takeout ingest job"
```

---

## Task 11: Import with immich-go

**Files:**
- Create: `immich/import/immich-go-job.yml`

**Interfaces:**
- Consumes: PVC `immich-takeout` (Task 10); Secret `immich-api` (Task 5); the in-cluster server Service.
- Produces: the imported library, with dates, albums, and GPS preserved.

> **Note on distribution:** immich-go publishes **release binaries only — there is no official container image.** Any plan or snippet referencing `ghcr.io/simulot/immich-go` is wrong. The Job below downloads and checksum-verifies the official tarball into a stock Debian image.
>
> Pinned for v0.32.0, `linux/amd64` (all four nodes are amd64):
> - URL: `https://github.com/simulot/immich-go/releases/download/v0.32.0/immich-go_Linux_x86_64.tar.gz`
> - SHA256: `6e2ad86bafdadb9466d6515de7cb882726c0aea1a21d51164dff361d7d480a97`

- [ ] **Step 1: Verify immich-go v0.32.0 command syntax locally**

The flag syntax must be confirmed against the actual release rather than assumed — it has changed across versions.

```bash
cd /tmp
curl -sSL -o immich-go.tar.gz \
  https://github.com/simulot/immich-go/releases/download/v0.32.0/immich-go_Linux_x86_64.tar.gz
echo "6e2ad86bafdadb9466d6515de7cb882726c0aea1a21d51164dff361d7d480a97  immich-go.tar.gz" | sha256sum -c -
tar xzf immich-go.tar.gz
./immich-go upload from-google-photos --help
```

Expected: `sha256sum -c` prints `OK`, and the help text lists the upload flags.

Record the exact flag names for server URL, API key, and the archive path argument. If they differ from the ones used in Step 2, update Step 2 to match the help output — the help text is authoritative, this plan is not.

- [ ] **Step 2: Create `immich/import/immich-go-job.yml`**

```yaml
---
apiVersion: batch/v1
kind: Job
metadata:
  name: immich-go-import
  namespace: immich
spec:
  backoffLimit: 0
  template:
    metadata:
      annotations:
        backup.velero.io/backup-volumes-excludes: takeout
    spec:
      restartPolicy: Never
      dnsConfig:
        options:
          - name: ndots
            value: "1"
      containers:
        - name: immich-go
          image: debian:bookworm-slim
          env:
            - name: IMMICH_API_KEY
              valueFrom:
                secretKeyRef:
                  name: immich-api
                  key: API_KEY
            - name: IMMICH_GO_VERSION
              value: "v0.32.0"
            - name: IMMICH_GO_SHA256
              value: "6e2ad86bafdadb9466d6515de7cb882726c0aea1a21d51164dff361d7d480a97"
          command:
            - /bin/bash
            - -c
            - |
              set -euo pipefail
              apt-get update -qq && apt-get install -y -qq --no-install-recommends curl ca-certificates
              cd /tmp
              curl -sSL -o immich-go.tar.gz \
                "https://github.com/simulot/immich-go/releases/download/${IMMICH_GO_VERSION}/immich-go_Linux_x86_64.tar.gz"
              echo "${IMMICH_GO_SHA256}  immich-go.tar.gz" | sha256sum -c -
              tar xzf immich-go.tar.gz
              chmod +x ./immich-go
              ./immich-go upload from-google-photos \
                --server=http://immich-server.immich.svc.cluster.local:2283 \
                --api-key="${IMMICH_API_KEY}" \
                ${DRY_RUN:+--dry-run} \
                /takeout/*.zip
          volumeMounts:
            - name: takeout
              mountPath: /takeout
              readOnly: true
          resources:
            requests:
              cpu: "1"
              memory: 1Gi
            limits:
              cpu: "2"
              memory: 4Gi
      volumes:
        - name: takeout
          persistentVolumeClaim:
            claimName: immich-takeout
```

`backoffLimit: 0` is deliberate: a partial import that auto-retries from the beginning wastes hours. Investigate failures manually instead.

The target is the in-cluster Service, not the Tailscale hostname — no reason to route hundreds of gigabytes through the tailnet proxy.

- [ ] **Step 3: Dry run first**

```bash
kubectl --context local-k3s create -f immich/import/immich-go-job.yml --dry-run=client -o yaml \
  | python3 -c "
import sys,yaml
d=yaml.safe_load(sys.stdin)
c=d['spec']['template']['spec']['containers'][0]
c.setdefault('env',[]).append({'name':'DRY_RUN','value':'1'})
d['metadata']['name']='immich-go-dryrun'
print(yaml.safe_dump(d))
" | kubectl --context local-k3s apply -f -

kubectl --context local-k3s logs -n immich -f job/immich-go-dryrun
```

Expected: a plausible asset count with no authentication or parse errors. Compare the count against your Google Photos total before proceeding.

- [ ] **Step 4: Run the real import**

```bash
kubectl --context local-k3s delete job immich-go-dryrun -n immich
kubectl --context local-k3s apply -f immich/import/immich-go-job.yml
kubectl --context local-k3s logs -n immich -f job/immich-go-import
```

This runs for many hours. There is no `activeDeadlineSeconds`, so it will not be killed mid-flight.

- [ ] **Step 5: Verify the import completed and counts are sane**

```bash
kubectl --context local-k3s wait --for=condition=complete job/immich-go-import -n immich --timeout=172800s
kubectl --context local-k3s exec -n immich immich-postgres-0 -- \
  psql -U immich -d immich -c "SELECT count(*) FROM assets;"
kubectl --context local-k3s exec -n immich immich-postgres-0 -- \
  psql -U immich -d immich -c "SELECT count(*) FROM albums;"
```

Expected: the asset count is close to the dry-run figure, and the album count is non-zero — a zero album count means the `.json` sidecars were not parsed, which defeats the purpose of using immich-go.

- [ ] **Step 6: Spot-check metadata quality in the UI**

Open the web UI and confirm on a sample of photos:
- capture dates are historical, **not** the import date (the single most common Takeout import failure)
- albums from Google Photos are present
- GPS/map data appears for photos that had it

If dates are all "today", stop — the sidecars were not read, and the fix is the immich-go invocation, not a re-import on top of bad data.

- [ ] **Step 7: Verify library files landed on the bulk pool**

```bash
kubectl --context local-k3s exec -n immich deploy/immich-server -- du -sh /usr/src/app/upload
```

Expected: a size on the order of the Takeout total.

- [ ] **Step 8: Commit**

```bash
git add immich/import/immich-go-job.yml
git commit -m "add immich-go import job"
```

---

## Task 12: Enable machine learning and clean up

**Files:**
- Modify: `immich/values.yml`

**Interfaces:**
- Consumes: the completed import (Task 11).
- Produces: CLIP search and facial recognition enabled; transient resources removed.

Because `IMMICH_CONFIG_FILE` is set, these settings are **read-only in the admin UI**. They must be changed in `values.yml` and applied with `helm upgrade`. Attempting this in the UI will appear to do nothing.

- [ ] **Step 1: Modify `immich/values.yml`**

Under `immich.configuration.machineLearning`, change both flags to `true` and drop the now-stale comment:

```yaml
    machineLearning:
      clip:
        enabled: true
      facialRecognition:
        enabled: true
```

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

Expected: both `clip.enabled` and `facialRecognition.enabled` are `true`. The server pod must restart to pick this up; confirm with `kubectl --context local-k3s rollout status deploy/immich-server -n immich`.

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

If node-1 or node-3 is saturated to the point of affecting other workloads, lower job concurrency in the admin UI rather than killing the pods.

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
kubectl --context local-k3s delete job immich-go-import immich-rclone-takeout -n immich --ignore-not-found
kubectl --context local-k3s delete -f immich/import/takeout-pvc.yml
```

`archiveOnDelete: "true"` means the NFS directory is archived rather than destroyed, so this is recoverable. Reclaim the space on the NAS manually once you are confident.

- [ ] **Step 8: Verify the bulk pool has room**

```bash
kubectl --context local-k3s exec -n immich deploy/immich-server -- df -h /usr/src/app/upload
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

**2. Placeholder scan.** One intentional placeholder remains: `__DB_STORAGE_TYPE__` in Task 3, which Task 2 exists specifically to resolve, and Task 3 Step 3 states the substitution explicitly. Two values require environment-specific input and say so at point of use: the Drive folder path (Task 10 Step 4) and the API key (Task 5 Step 5). No `TBD`, `TODO`, or "handle errors appropriately" instructions.

**2a. External reference verification.** Every image and metric referenced was checked against its registry or against the live cluster, not recalled:

| Reference | Status |
|---|---|
| `oci://ghcr.io/immich-app/immich-charts/immich:0.13.2` | verified, appVersion v3.2.0 |
| `ghcr.io/immich-app/postgres:17-vectorchord1.1.1` | verified, HTTP 200 |
| `rclone/rclone:1.68`, `curlimages/curl:8.10.1`, `busybox:1.36`, `debian:bookworm-slim` | verified |
| `ghcr.io/simulot/immich-go` | **does not exist — corrected.** immich-go ships release binaries only; Task 11 now downloads and checksum-verifies the tarball |
| `prometheus-kube-prometheus-stack-prometheus-0` | verified, running |
| `kube_cronjob_status_last_successful_time`, `kube_statefulset_status_replicas_ready`, `kubelet_volume_stats_{used,capacity}_bytes` | all present; kube-state-metrics is deployed |
| Chart values keys (`server.service.main.annotations`, `defaultPodOptions.dnsConfig`, `immich.persistence.library.existingClaim`, `immich.configuration`) | verified by rendering chart 0.13.2 |
| `{{y}}` in values survives Helm without rendering | verified by rendering |

The immich-go image was a genuine error in the first draft of this plan — the kind that would have failed at `ImagePullBackOff` after the multi-hour rclone transfer had already completed.

**3. Name consistency.** Verified across tasks:
- PVCs: `immich-library`, `immich-ml-cache`, `immich-pgdump`, `immich-postgres-data`, `immich-valkey-data`, `immich-takeout` — consistent in Tasks 1, 3, 4, 6, 8, 10, 11, 12.
- Secrets: `immich-postgres` (3), `immich-api` (5, 11), `immich-rclone` (10).
- Services and ports: `immich-server:2283`, `immich-machine-learning:3003`, `immich-valkey:6379`, `immich-postgres:5432` — all verified against rendered chart output.
- Velero volume names in exclusion annotations (`data`, `cache`, `takeout`) match the volume names in the pod specs that define them, checked in Task 8 Step 1.
- ConfigMap `immich-immich-config` matches the rendered chart name.

**4. Ordering.** Tasks 1–9 are the steady-state deployment and can run back-to-back. Tasks 10–12 are gated on the Google Takeout export existing and are explicitly marked. Task 8 depends on annotations created in Tasks 3 and 4 and verifies them before acting, rather than assuming.
