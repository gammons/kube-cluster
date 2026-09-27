# CLIProxyAPI Kubernetes Deployment Design

## Date

2026-09-23

## Goal

Run CLIProxyAPI as a shared homelab service on the `local-k3s` Kubernetes cluster so multiple LAN clients can use one stable Anthropic/Claude-compatible endpoint backed by multiple Claude OAuth subscriptions.

## Approved approach

Use a single-replica Kubernetes Deployment with an NFS-backed auth PVC and a MetalLB LoadBalancer Service.

This is intentionally simpler than a highly available multi-replica deployment. CLIProxyAPI currently stores OAuth credentials as files and watches that directory for changes. A single replica with durable shared storage matches that behavior without introducing PostgreSQL, Object Storage, or Git state coordination.

## Context

- Cluster context: `local-k3s`
- Manifest location: `cliproxyapi/` in this repository
- Storage classes available: `local-path`, `nfs`, `nfs-bulk`
- MetalLB address pool: `192.168.20.1-192.168.20.255`
- Proposed service IP: `192.168.20.17`
- Container image: `eceasy/cli-proxy-api:v7.3.15`
- Desired routing behavior: drain one Claude subscription first, then fail over to the next

## Design

### Namespace

Create a dedicated `cliproxyapi` namespace.

### Storage

Create one PVC:

- Name: `cliproxyapi-auth`
- Storage class: `nfs`
- Size: `1Gi`
- Access mode: `ReadWriteMany`

Mount it at `/auth` in the container. This directory contains OAuth token JSON files and must persist across pod restarts and rescheduling.

Application logs should go to stdout/stderr rather than a log PVC.

### Configuration

Create a Kubernetes Secret mounted as `/config/config.yaml`.

Initial config:

```yaml
host: ""
port: 8317
auth-dir: "/auth"

api-keys:
  - "<generated-out-of-band>"

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

The Management API is disabled initially. OAuth account setup uses CLI login flows rather than remote management endpoints.

The real Secret is not committed. The repository contains `secret.example.yml` only.

### Deployment

Create a Deployment with:

- 1 replica
- `Recreate` strategy
- image `eceasy/cli-proxy-api:v7.3.15`
- config Secret mounted at `/config/config.yaml`
- auth PVC mounted at `/auth`
- stdout logging
- `/healthz` probe
- modest resource requests/limits

### Service

Create a MetalLB Service:

- Type: `LoadBalancer`
- IP: `192.168.20.17`
- Port: `8317`
- Target port: `8317`

Client base URL:

```text
http://192.168.20.17:8317/v1
```

Only the main API port is exposed. OAuth callback ports are not exposed through the Service.

### Tailnet Service (added 2026-09-23)

A second, separate `ClusterIP` Service, `cliproxyapi-tailscale` (`cliproxyapi/tailscale-service.yml`), is annotated `tailscale.com/expose: "true"` and `tailscale.com/hostname: "cliproxyapi"`. The Tailscale operator runs a proxy pod that joins the tailnet, giving an endpoint that also works away from the home network:

```text
http://cliproxyapi.rya-scala.ts.net:8317/v1
```

This follows the existing `immich-server` / `loki-tailscale` pattern. Keeping it separate from the MetalLB Service lets either exposure change without affecting the other. Tailnet traffic is WireGuard-encrypted. The API key is left as-is because the tailnet has a single user.

## Multi-account Claude setup

For each Claude subscription:

1. Port-forward the callback port:

   ```bash
   kubectl --context local-k3s -n cliproxyapi port-forward deploy/cliproxyapi 54545:54545
   ```

2. In another terminal, start the login flow:

   ```bash
   kubectl --context local-k3s -n cliproxyapi exec -it deploy/cliproxyapi -- \
     /CLIProxyAPI/CLIProxyAPI -config /config/config.yaml -claude-login --no-browser
   ```

3. Open the printed OAuth URL in a local browser.

4. Repeat for each Claude account.

The running server watches `/auth` and picks up new token files automatically.

## Client configuration

Clients such as OpenCode should use the MetalLB endpoint and a configured proxy API key.

Example Anthropic provider settings:

```yaml
baseURL: "http://192.168.20.17:8317/v1"
apiKey: "<generated-out-of-band>"
```

## Migration

If preserving existing local auth state is desired, copy the existing local CLIProxyAPI auth token files into the new PVC before switching clients.

No automatic cutover is required. Local and Kubernetes proxies can coexist temporarily while clients are updated one at a time.

## Security

- The proxy API key must be long and random.
- The MetalLB endpoint is plain HTTP, so it must remain LAN-only.
- The Management API is disabled initially.
- OAuth token files live on an NFS-backed PVC and should be treated as sensitive.
- Do not commit real keys, tokens, or auth JSON files.

## Error handling and operations

- If one Claude account hits quota, `fill-first` routing should move to the next available account.
- If a token expires or becomes invalid, re-run the Claude login flow for that account.
- If the pod is rescheduled, the NFS PVC preserves auth state.
- If the service IP changes, client configs must be updated.

## Testing

1. Apply namespace, PVC, Secret, Deployment, and Service with `kubectl --context local-k3s`.
2. Verify the pod is running and the Service has `192.168.20.17`.
3. Authenticate at least one Claude account through the port-forward login flow.
4. Verify `/v1/models` returns Claude models when called with the proxy API key.
5. Send a minimal Anthropic Messages request and verify a successful response.
6. Authenticate a second Claude account and verify the auth file appears in `/auth`.
7. Optionally test failover behavior by manually cooling down or removing one account and observing fallback.

## Non-goals

- Multi-replica HA
- PostgreSQL/Object Storage/Git shared state
- Public internet exposure
- TLS termination
- Management UI/API enablement
- Automatic migration of existing local auth files
