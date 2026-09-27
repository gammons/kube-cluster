# CLIProxyAPI Kubernetes Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Run CLIProxyAPI on the `local-k3s` cluster as a shared LAN Anthropic/Claude-compatible proxy backed by durable NFS auth storage.

**Architecture:** Single-replica Deployment, NFS PVC for OAuth token files, Secret-mounted config, and MetalLB LoadBalancer Service. Claude accounts are added through repeated CLI OAuth logins; the server watches `/auth` and rotates/fails over according to the config.

**Tech Stack:** Kubernetes manifests, k3s, MetalLB, NFS PVC, `eceasy/cli-proxy-api:v7.3.15`

**Spec:** `docs/superpowers/specs/2026-09-23-cliproxyapi-kubernetes-design.md`

## Global Constraints

- Use kubectl context `local-k3s` for every cluster command.
- Put manifests in `cliproxyapi/` under this repository.
- Use image `eceasy/cli-proxy-api:v7.3.15`.
- Use namespace `cliproxyapi`.
- Use MetalLB IP `192.168.20.17` on port `8317`.
- Use storage class `nfs` for the auth PVC.
- Do not commit real API keys, OAuth tokens, or generated `secret.yml`.
- Keep Management API disabled initially: `remote-management.secret-key: ""`.

## Review Focus

- The Service must actually receive `192.168.20.17`; otherwise clients will not have the documented stable endpoint.
- The auth PVC must be mounted at `/auth`; otherwise OAuth tokens disappear on pod restart.
- Config must use `routing.strategy: fill-first` and `quota-exceeded.switch-project: true`; otherwise the proxy will not drain/fail over subscriptions as intended.
- The real config Secret must exist before the Deployment is applied; otherwise the pod will fail to start.
- Only port `8317` is exposed; OAuth callback and management endpoints must not be publicly reachable.

---

### Task 1: Add CLIProxyAPI manifests and documentation

**Files:**
- Create: `cliproxyapi/.gitignore`
- Create: `cliproxyapi/ns.yml`
- Create: `cliproxyapi/pvc.yml`
- Create: `cliproxyapi/secret.example.yml`
- Create: `cliproxyapi/deployment.yml`
- Create: `cliproxyapi/service.yml`
- Create: `cliproxyapi/README.md`
- Modify: `README.md`

**Interfaces:**
- Consumes: none.
- Produces: manifest directory and file names used by later tasks.

- [ ] **Step 1: Create the manifest directory**

Run:

```bash
mkdir -p /home/dev/local_code/kube-cluster/cliproxyapi
```

Expected: directory exists.

- [ ] **Step 2: Write `.gitignore`**

Create `cliproxyapi/.gitignore`:

```gitignore
secret.yml
```

- [ ] **Step 3: Write namespace manifest**

Create `cliproxyapi/ns.yml`:

```yaml
apiVersion: v1
kind: Namespace
metadata:
  name: cliproxyapi
```

- [ ] **Step 4: Write auth PVC manifest**

Create `cliproxyapi/pvc.yml`:

```yaml
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: cliproxyapi-auth
  namespace: cliproxyapi
spec:
  storageClassName: nfs
  accessModes:
    - ReadWriteMany
  resources:
    requests:
      storage: 1Gi
```

- [ ] **Step 5: Write example Secret manifest**

Create `cliproxyapi/secret.example.yml`:

```yaml
apiVersion: v1
kind: Secret
metadata:
  name: cliproxyapi-config
  namespace: cliproxyapi
type: Opaque
stringData:
  config.yaml: |
    host: ""
    port: 8317
    auth-dir: "/auth"

    api-keys:
      - "REPLACE_WITH_LONG_RANDOM_KEY"

    routing:
      strategy: "fill-first"

    quota-exceeded:
      switch-project: true
      switch-preview-model: false

    request-retry: 3
    max-retry-credentials: 5
    max-retry-interval: 30

    remote-management:
      allow-remote: false
      secret-key: ""
```

- [ ] **Step 6: Write Deployment manifest**

Create `cliproxyapi/deployment.yml`:

```yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: cliproxyapi
  namespace: cliproxyapi
spec:
  replicas: 1
  strategy:
    type: Recreate
  selector:
    matchLabels:
      app: cliproxyapi
  template:
    metadata:
      labels:
        app: cliproxyapi
    spec:
      containers:
        - name: cliproxyapi
          image: eceasy/cli-proxy-api:v7.3.15
          command:
            - /CLIProxyAPI/CLIProxyAPI
            - -config
            - /config/config.yaml
          ports:
            - name: http
              containerPort: 8317
          readinessProbe:
            httpGet:
              path: /healthz
              port: http
            initialDelaySeconds: 5
            periodSeconds: 10
          livenessProbe:
            httpGet:
              path: /healthz
              port: http
            initialDelaySeconds: 15
            periodSeconds: 20
          resources:
            requests:
              cpu: 100m
              memory: 256Mi
            limits:
              cpu: 500m
              memory: 1Gi
          volumeMounts:
            - name: config
              mountPath: /config
              readOnly: true
            - name: auth
              mountPath: /auth
      volumes:
        - name: config
          secret:
            secretName: cliproxyapi-config
            optional: false
        - name: auth
          persistentVolumeClaim:
            claimName: cliproxyapi-auth
```

- [ ] **Step 7: Write MetalLB Service manifest**

Create `cliproxyapi/service.yml`:

```yaml
apiVersion: v1
kind: Service
metadata:
  name: cliproxyapi
  namespace: cliproxyapi
  annotations:
    metallb.universe.tf/loadBalancerIPs: 192.168.20.17
spec:
  type: LoadBalancer
  selector:
    app: cliproxyapi
  ports:
    - name: http
      port: 8317
      targetPort: http
```

- [ ] **Step 8: Write README**

Create `cliproxyapi/README.md`:

```markdown
# CLIProxyAPI

Shared LAN proxy for Claude/Anthropic-compatible clients.

## Apply

Create the real Secret first:

```bash
cp secret.example.yml secret.yml
# replace REPLACE_WITH_LONG_RANDOM_KEY with a long random value
kubectl --context local-k3s apply -f ns.yml
kubectl --context local-k3s apply -f pvc.yml
kubectl --context local-k3s apply -f secret.yml
kubectl --context local-k3s apply -f deployment.yml
kubectl --context local-k3s apply -f service.yml
```

Do not commit `secret.yml`.

## Client endpoint

```text
http://192.168.20.17:8317/v1
```

Use the API key from `secret.yml` as the bearer/API key.

## Add Claude accounts

Terminal 1:

```bash
kubectl --context local-k3s -n cliproxyapi port-forward deploy/cliproxyapi 54545:54545
```

Terminal 2:

```bash
kubectl --context local-k3s -n cliproxyapi exec -it deploy/cliproxyapi -- \
  /CLIProxyAPI/CLIProxyAPI -config /config/config.yaml -claude-login --no-browser
```

Open the printed OAuth URL in a browser. Repeat for each Claude subscription.

## Verify

```bash
KEY=$(awk '/- "/ {gsub(/"/, "", $2); print $2; exit}' secret.yml)
curl -fsS http://192.168.20.17:8317/healthz
curl -fsS http://192.168.20.17:8317/v1/models \
  -H "Authorization: Bearer $KEY"
```

The second command returns Claude models after at least one successful Claude login.
```

- [ ] **Step 9: Add the service to the repository root README**

Append this line to the root `README.md` service list:

```markdown
### CLIProxyAPI

1. Follow `cliproxyapi/README.md`.
```

- [ ] **Step 10: Client-side validate the manifests**

Run from `/home/dev/local_code/kube-cluster`:

```bash
kubectl --context local-k3s apply --dry-run=client -f cliproxyapi/ns.yml
kubectl --context local-k3s apply --dry-run=client -f cliproxyapi/pvc.yml
kubectl --context local-k3s apply --dry-run=client -f cliproxyapi/secret.example.yml
kubectl --context local-k3s apply --dry-run=client -f cliproxyapi/deployment.yml
kubectl --context local-k3s apply --dry-run=client -f cliproxyapi/service.yml
```

Expected: all commands exit 0.

### Task 2: Create the real Secret and deploy base resources

**Files:**
- Create: `cliproxyapi/secret.yml` (ignored by git)

**Interfaces:**
- Consumes: `cliproxyapi/secret.example.yml` from Task 1.
- Produces: live namespace, PVC, and `cliproxyapi-config` Secret used by Task 3.

- [ ] **Step 1: Generate a client API key**

Run:

```bash
openssl rand -hex 32
```

Expected: one long random hex string.

- [ ] **Step 2: Create the ignored Secret manifest**

Run from `/home/dev/local_code/kube-cluster`:

```bash
cp cliproxyapi/secret.example.yml cliproxyapi/secret.yml
python3 - <<'PY'
from pathlib import Path
import secrets
p = Path('cliproxyapi/secret.yml')
s = p.read_text()
s = s.replace('REPLACE_WITH_LONG_RANDOM_KEY', secrets.token_hex(32))
p.write_text(s)
PY
```

Expected: `cliproxyapi/secret.yml` exists and is ignored by git.

- [ ] **Step 3: Apply namespace and PVC**

Run:

```bash
kubectl --context local-k3s apply -f cliproxyapi/ns.yml
kubectl --context local-k3s apply -f cliproxyapi/pvc.yml
```

Expected: both resources are created or unchanged.

- [ ] **Step 4: Apply the real Secret**

Run:

```bash
kubectl --context local-k3s apply -f cliproxyapi/secret.yml
```

Expected: Secret `cliproxyapi/cliproxyapi-config` is created or updated.

- [ ] **Step 5: Server-side validate workload manifests**

Run:

```bash
kubectl --context local-k3s apply --dry-run=server -f cliproxyapi/deployment.yml
kubectl --context local-k3s apply --dry-run=server -f cliproxyapi/service.yml
```

Expected: both commands exit 0.

### Task 3: Deploy workload and verify LAN endpoint

**Files:**
- Modify: none.

**Interfaces:**
- Consumes: live namespace/PVC/Secret from Task 2.
- Produces: running `cliproxyapi` Deployment and Service at `192.168.20.17:8317`.

- [ ] **Step 1: Apply Deployment and Service**

Run:

```bash
kubectl --context local-k3s apply -f cliproxyapi/deployment.yml
kubectl --context local-k3s apply -f cliproxyapi/service.yml
```

Expected: both resources are created or unchanged.

- [ ] **Step 2: Wait for rollout**

Run:

```bash
kubectl --context local-k3s -n cliproxyapi rollout status deploy/cliproxyapi --timeout=120s
```

Expected: deployment successfully rolled out.

- [ ] **Step 3: Verify the Service IP**

Run:

```bash
kubectl --context local-k3s -n cliproxyapi get svc cliproxyapi
```

Expected: `TYPE` is `LoadBalancer` and `EXTERNAL-IP` is `192.168.20.17`.

- [ ] **Step 4: Verify health without printing the key**

Run from `/home/dev/local_code/kube-cluster`:

```bash
curl -fsS http://192.168.20.17:8317/healthz
```

Expected: successful HTTP response from the proxy.

### Task 4: Authenticate the first Claude account and verify Claude models

**Files:**
- Modify: none.

**Interfaces:**
- Consumes: running proxy from Task 3 and API key from `cliproxyapi/secret.yml`.
- Produces: first persisted Claude OAuth token in `/auth` and a model list containing Claude models.

- [ ] **Step 1: Start the OAuth callback port-forward**

Run:

```bash
kubectl --context local-k3s -n cliproxyapi port-forward deploy/cliproxyapi 54545:54545
```

Expected: port-forward remains running. Leave this terminal open.

- [ ] **Step 2: Start the Claude login flow**

In a second terminal, run:

```bash
kubectl --context local-k3s -n cliproxyapi exec -it deploy/cliproxyapi -- \
  /CLIProxyAPI/CLIProxyAPI -config /config/config.yaml -claude-login --no-browser
```

Expected: a Claude OAuth URL is printed. Open it in a local browser and complete login.

- [ ] **Step 3: Verify auth file persistence**

Run:

```bash
kubectl --context local-k3s -n cliproxyapi exec deploy/cliproxyapi -- ls -la /auth
```

Expected: at least one Claude/Anthropic OAuth JSON file exists.

- [ ] **Step 4: Verify Claude models are visible**

From `/home/dev/local_code/kube-cluster`, run:

```bash
KEY=$(awk '/- "/ {gsub(/"/, "", $2); print $2; exit}' cliproxyapi/secret.yml)
curl -fsS http://192.168.20.17:8317/v1/models \
  -H "Authorization: Bearer $KEY" | grep -i claude
```

Expected: output includes at least one Claude model.

### Task 5: Verify repository state and summarize cutover

**Files:**
- Modify: none.

**Interfaces:**
- Consumes: completed manifests and live cluster smoke tests.
- Produces: clean working tree summary and remaining manual cutover notes.

- [ ] **Step 1: Confirm generated Secret is ignored**

Run from `/home/dev/local_code/kube-cluster`:

```bash
git check-ignore cliproxyapi/secret.yml
git status --short
```

Expected: `cliproxyapi/secret.yml` is ignored; generated manifests/docs are listed as untracked or modified.

- [ ] **Step 2: Summarize remaining client cutover**

Confirm these values are ready for clients:

```text
baseURL: http://192.168.20.17:8317/v1
apiKey: value from cliproxyapi/secret.yml
```

Do not print the key in chat or commit it.
