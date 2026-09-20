# Immich LAN access via MetalLB

Date: 2026-09-18
Status: approved, ready for implementation planning
Cluster: `local-k3s`
Amends: `docs/superpowers/specs/2026-09-12-immich-homelab-design.md`

## Goal

Let a second household phone use the Immich mobile app without installing or
maintaining Tailscale on it. Expose `immich-server` on a stable IP reachable
from the home WiFi, alongside — not instead of — the existing Tailscale access.

## Context: verified cluster state

Facts checked against the live cluster, not recalled.

| Property | Value |
|---|---|
| MetalLB | installed, `controller` + 3 `speaker` pods Running in `metallb-system` |
| Pool | `main-pool`, `192.168.20.1-192.168.20.255`, `autoAssign: true` |
| Advertisement | `main-pool-advertisement`, L2, all interfaces |
| Allocated IPs | `.1` traefik, `.2` wprb-rocks, `.4` + `.10` unifi, `.50` registry |
| Node addresses | `192.168.10.1-4` (a different subnet from the pool) |
| `immich-server` Service | `ClusterIP`, ports `http` 2283, `metrics-api` 8081, `metrics-ms` 8082 |
| Its selector | `app.kubernetes.io/{name=server,instance=immich,controller=main}` |
| Container ports | `http`=2283, `metrics-api`=8081, `metrics-ms`=8082 |
| Existing pinning convention | `lb-unifi` uses the **annotation** `metallb.universe.tf/loadBalancerIPs: 192.168.20.10`; `spec.loadBalancerIP` is empty |

`192.168.20.0/24` is already reachable from the LAN — the UniFi controller and
container registry are consumed that way daily.

## Requirements

1. A stable, unchanging IP the mobile app can be pointed at.
2. Existing Tailscale access (`immich.rya-scala.ts.net:2283`) must keep working.
3. `immich-server.immich.svc.cluster.local:2283` must keep resolving — the
   import Jobs, the PrometheusRule and the README runbook all depend on it.
4. Only the API port is published to the LAN. Metrics stay internal.
5. Nothing published to the public internet.
6. Reversible without touching the Helm release.

## Decision: a hand-written LoadBalancer Service

Create `immich/lan-service.yml` defining one additional Service that selects the
same pods. The chart-managed `immich-server` Service is left completely alone.

### Rejected: adding the service through the chart

`server.service.lan` in `values.yml` is the idiomatic route and was tried first.
Rendering it proved two blocking side effects:

1. **It renames the existing Service `immich-server` → `immich-server-main`.**
   That name is what the Tailscale operator's proxy tracks and what every
   in-cluster URL resolves, so the rename would break requirements 2 and 3.
2. It breaks the chart's ServiceMonitor, which can no longer auto-detect its
   target: *"Either 'service.name' or 'service.identifier' is required because
   automatic Service detection is not possible (found 2 enabled services)."*
   Workable via `server.serviceMonitor.main.service.identifier`, but only after
   accepting the rename.

Chart-managed configuration is normally preferred in this repo. It is not worth
those two costs here.

### Rejected: switching the existing Service to `LoadBalancer`

Fewest objects, but it publishes `metrics-api` 8081 and `metrics-ms` 8082 on the
LAN IP alongside 2283, violating requirement 4, and entangles the Tailscale and
MetalLB configuration on a single chart-managed object.

## The Service

| Field | Value | Why |
|---|---|---|
| `metadata.name` | `immich-server-lan` | distinct from the chart's `immich-server` |
| `type` | `LoadBalancer` | MetalLB allocates from `main-pool` |
| annotation | `metallb.universe.tf/loadBalancerIPs: 192.168.20.20` | free; matches the `lb-unifi` convention. `spec.loadBalancerIP` is deprecated and not used |
| `selector` | the three `app.kubernetes.io` labels above | tracks the pod across rollouts |
| `ports` | `2283` → `targetPort: http` | named target port survives a future renumbering |
| `externalTrafficPolicy` | omitted, i.e. `Cluster` | more robust than `Local` with a single replica pinned to one node |

Metrics ports are deliberately absent.

`192.168.20.20` is chosen over auto-assignment because the mobile app stores the
URL; an address that moved after a restart would silently break backup.

## Deviation from the approved design

`2026-09-12-immich-homelab-design.md` states access is *"Tailscale-only"* and
that a dropped VPN pauses mobile backup. This spec widens access to the LAN.

- The *"nothing published to the public internet"* requirement is **unchanged**
  and still met. This is an RFC1918 address on the home network.
- Anyone on the WiFi — guests, IoT devices sharing the subnet — can now reach
  the Immich login page over plain HTTP. Immich still requires authentication.
  This matches how `registry` and the UniFi controller are already exposed.
- No TLS. Accepted: the traffic is LAN-local and the alternative (cert-manager
  plus a local CA trusted by the phone) is disproportionate here.

Accepted consciously, recorded so a future reader does not treat it as drift.

## Consequences for mobile backup

Background backup runs only on the home WiFi and pauses elsewhere. This is a
deliberate simplification over the Tailscale path, whose documented trade-off was
that a dropped VPN — common on iOS — pauses backup anyway.

Both phones upload into the **existing single admin account**, matching current
usage. No second Immich user is created. Uploads land in the same library and
timeline and are subject to the same SMR write characteristics.

## Verification

1. `kubectl --context local-k3s get svc immich-server-lan -n immich` shows
   `EXTERNAL-IP 192.168.20.20`.
2. From a device on the WiFi: `curl http://192.168.20.20:2283/api/server/ping`
   returns `{"res":"pong"}`.
3. Port 8081 on that IP refuses connection, proving metrics are not published.
4. The chart Service is untouched: `kubectl get svc immich-server -n immich`
   still exists as `ClusterIP` and still carries the two `tailscale.com`
   annotations.
5. `http://immich.rya-scala.ts.net:2283/api/server/ping` still returns pong.
6. The Immich app on the phone connects to `http://192.168.20.20:2283` and
   completes a login.

## Rollback

`kubectl --context local-k3s delete -f immich/lan-service.yml`. Nothing else is
affected; the Helm release is not involved.

## Out of scope

- TLS, cert-manager, or a local CA.
- A second Immich user account, or per-user album separation.
- Any public-internet exposure, port forwarding, or DNS.
- Access from outside the home network, which remains Tailscale's job.
- A DNS name for the LAN IP. The app stores a URL once; a hostname would add a
  dependency on local DNS for no practical gain.
