# k3s Homelab Cluster Upgrade — Design

**Date:** 2026-09-12
**Cluster:** `local-k3s` (kubectl context), 4 VMs on Proxmox host `pve` (192.168.5.1)
**Scope approved:** Phases 0-4. Phase 5+ (Kubernetes minor hops) deferred pending reassessment.

> **Always pass `--context local-k3s` explicitly.** The kubeconfig contains 8 contexts
> including `aws-truelist-prod` and `production`, and `current-context` has previously
> pointed at a remote production cluster.

---

## Current State

All "latest" figures below were verified against upstream repos on 2026-09-12, not
assumed.

| Component | Installed | Latest | Gap | Managed by |
|---|---|---|---|---|
| k3s / Kubernetes | **v1.29.4+k3s1** | v1.36.4+k3s1 | **7 minors** | install script, systemd |
| Tailscale operator | **1.76.6** (2024-11-10) | **1.102.3** | **26 releases** | Helm (`tailscale/tailscale-operator`) |
| MetalLB | **v0.14.5** | **v0.16.1** | 2 minors | **raw `kubectl apply`** (not Helm) |
| cert-manager | **v1.14.5** (2024-05) | **v1.21.2** | **7 minors** | Helm (`jetstack/cert-manager`) |
| Traefik | v2.10.5 (chart 25.0.3) | v3.x | 1 major | k3s packaged component |
| Velero | 1.18.1 | — | — | Helm |

**Nodes:** 4 × Ubuntu 24.04, kernel 6.8.0-139, all `v1.29.4+k3s1`, age 2y122d.
`k3s-controller` (192.168.10.1) is `Ready,SchedulingDisabled`; workers `k3s-node-1/2/3`
at 192.168.10.2/.3/.4. Reachable over Tailscale by hostname.

**Datastore:** `k3s server` runs with **no arguments** → SQLite (kine), single server.
No etcd, therefore no built-in snapshot tooling, and no control-plane HA.

---

## Scope Reality Check

The Kubernetes item is not one upgrade. **v1.29.4 → v1.36.4 is seven minor versions**,
and Kubernetes supports only one minor at a time for the control plane. That is 7
sequential upgrades across 4 nodes with verification at each step. The cluster is also
behind on patches *within* 1.29 (`.4` vs `.15`).

With a single control-plane node, every hop incurs API server downtime.

---

## Rollback Strategy (the enabler)

Velero is **not** a viable rollback mechanism here (see Findings). The nodes are
Proxmox VMs backed by ZFS, which gives something much stronger:

| Data | Lives on | Snapshot mechanism |
|---|---|---|
| k3s state (SQLite), all 23 `local-path` PVCs | VM zvols in `main-pool` | `qm snapshot <vmid>` |
| All 15 `nfs` PVCs | `main-pool/k3s-nfs` | `zfs snapshot main-pool/k3s-nfs@...` |

`main-pool` is at **58% capacity** with **660G available to datasets** (`zpool` reports
766G `FREE`; the 660G `zfs AVAIL` figure is the conservative one to plan against).
Snapshots are cheap — copy-on-write, so cost grows only with divergence during the
upgrade window. Snapshotting all four VMs plus the NFS dataset captures the entire
cluster state atomically and reverts in minutes.

**Every phase gets its own snapshot gate.** This converts a risky migration into a
series of individually reversible steps.

**Prerequisite:** VMs have **no `qemu-guest-agent`**, so snapshots would be
crash-consistent only, and two VMs (102/103) already ignored ACPI shutdown and needed
hard stops. Installing the agent is required before relying on snapshots.

**The rollback procedure must itself be tested in Phase 0**, before it is depended on.

---

## Findings

### Backups are far narrower than they appear

Velero reports 7 days of `Completed` daily backups with 447 items and 105
`PodVolumeBackups` (kopia), and `--default-volumes-to-fs-backup` is set server-side.
But the schedule is:

```json
{"includedNamespaces":["home-assistant","dev-box","openclaw","openclaw-dottie",
                       "openclaw-stonk","immich"],"ttl":"168h"}
```

**6 of ~31 namespaces.** Not covered: `monitoring` (Prometheus/Grafana/Loki/Alertmanager),
`truelist-staging` (**MariaDB + Redis**), `infra` (registry), `unifi`, `wprb-rocks`,
`pmbot`, `cert-manager`, `metallb-system`, `kube-system`, `outrank`, `tailscale`,
`nex-ezclaw-sandbox`, `preview-pr-demo`. Retention only 7 days.

Confirmed not a "pods weren't running" artifact — `mariadb-0` and all four monitoring
pods are Running with uncovered `local-path` PVCs.

Source of truth for the schedule is `../../../velero/values.yml`.

### MetalLB is load-bearing — do not remove it

Question raised: has Tailscale replaced MetalLB? **No.** They serve different purposes.

MetalLB provides LAN LoadBalancer IPs from pool `192.168.20.1-255`:

| Service | IP | Impact if lost |
|---|---|---|
| `kube-system/traefik` | 192.168.20.1 | **All 6 Ingresses** (grafana, truelist-staging, wprb.rocks, outrank, pr-demo) |
| `unifi/lb-unifi` | 192.168.20.10 | UniFi network controller |
| `unifi/lb-unifi-udp` | 192.168.20.4 | UniFi device discovery |
| `infra/registry` | 192.168.20.50 | Container registry |
| `wprb-rocks/wprb-rocks-backend-service` | 192.168.20.2 | wprb.rocks backend |

The Tailscale operator instead provides ~25 **egress** proxies so this cluster can
scrape Prometheus metrics from the OVH/production clusters. Complementary, not
overlapping. Both are needed.

### k3s ServiceLB (klipper) conflicts with MetalLB

Because `k3s server` runs with no args, the packaged ServiceLB is enabled and creates
hostPort DaemonSets for every LoadBalancer service, in parallel with MetalLB:

```
svclb-traefik-77f17da4-fb7nw    2/2 Running  104 restarts  2y122d
svclb-lb-unifi-55c44091-2bcs2   0/6 Pending    650d
```

`svclb-lb-unifi` needs 6 hostPorts (UniFi: 80/443/8080/8443/8843/8880) but
`svclb-traefik` already holds 80/443 — unschedulable for **650 days**. High restart
counts on `svclb-traefik` suggest further instability.

MetalLB performs the actual IP assignment. klipper is redundant and harmful.
Fix: `--disable=servicelb`.

### Traefik v2 → v3 risk is largely defused

Zero `IngressRoute`, `Middleware`, `TraefikService`, or `TLSOption` custom resources
exist — only 6 plain `Ingress` objects with `class: traefik`. Plain Ingress is stable
across the v2→v3 boundary. CRDs for both `traefik.containo.us` (removed in v3) and
`traefik.io` exist but hold no CRs, so their removal is harmless.

This matters because k3s reapplies its packaged Traefik chart on restart; a k3s
upgrade will bump Traefik whether or not we ask it to.

### cert-manager is a hard gate

v1.14.5 supports Kubernetes ≤1.29. Its **validating/mutating webhooks** will fail
against a 1.30+ API server, which would break all Certificate/Issuer admission.
Must be upgraded before any Kubernetes minor hop.

Existing issue: `truelist-staging/truelist-stag-io-cert` has been `READY=False` for
**171 days**, with a `cm-acme-http-solver-sqcfr` pod and orphan solver Ingress still
present. Diagnose before upgrading cert-manager so a pre-existing failure isn't
mistaken for upgrade fallout.

### Deprecated API usage: inconclusive, needs re-check

`apiserver_requested_deprecated_apis` is absent from `/metrics`, but the API server
had only been up ~2.5h after a reboot, so infrequent CronJobs would not yet have
registered. Not evidence of safety.

Direct probe of served groups: `policy/v1beta1`, `extensions/v1beta1`,
`autoscaling/v2beta2`, `batch/v1beta1`, `storage.k8s.io/v1beta1`,
`admissionregistration.k8s.io/v1beta1` are **not served** (already gone — good).
`flowcontrol.apiserver.k8s.io/v1beta3` **is** served and is removed in 1.32, but it is
apiserver-internal (APF) and self-migrates.

**Action:** re-sample the metric after ≥7 days of uptime before Phase 5.

### Other blockers and hazards

| Item | Detail |
|---|---|
| **Node root access** | SSH works as `grant` via Tailscale hostnames; **no passwordless sudo**. Password at `~/sudo-pw.txt` (use authorized 2026-09-12). Never echo it into logs or command lines that persist. |
| **Controller disk tight** | `/` is 31G with **9.2G free (69%)**. VM disk is 65G but LVM claims only 31G — room to extend. |
| **Failed Helm releases** | `home-assistant` rev 7 (2026-04-16), `openclaw-stonk` rev 4 (2026-06-17) |
| **`local-path` pins pods to nodes** | 23 PVCs use `local-path` with `WaitForFirstConsumer`. Draining a node makes those pods unschedulable until it returns — per-node downtime is unavoidable, not a misconfiguration. |
| **Ambiguous `backup` resource** | Leftover Longhorn CRDs (`backups.longhorn.io`) from the 2y-`Terminating` `longhorn-system` namespace shadow Velero's. **Always fully-qualify `backups.velero.io`.** |
| **Stuck namespaces** | `signoz`, `longhorn-system` `Terminating` for 2y+ (likely finalizers) |
| **Duplicate Loki** | `loki-stack 2.10.2` in `logging` + `loki 6.51.0` in `monitoring` |
| **Orphan CRDs** | `configuration.konghq.com` CRDs with no Kong install |

---

## Phases

Dependency-ordered, lowest risk first, so the snapshot/rollback process is proven
before the cluster is bet on it.

### Phase 0 — Prerequisites and hygiene (no version changes)

1. Confirm `sudo` works on all 4 nodes via the documented password.
2. Install `qemu-guest-agent` in all 4 guests; `qm set <id> --agent 1`. Verify
   `qm agent <id> ping` responds. **Required for clean snapshots.**
3. Establish the snapshot procedure **and test a real rollback** on the least critical
   node before trusting it.
4. Widen the Velero schedule to all namespaces as a secondary safety net.
5. Extend controller `/` (LVM has headroom inside the 65G zvol).
6. Resolve the two failed Helm releases.
7. Add `--disable=servicelb` to the k3s server unit; confirm the 5 `svclb-*`
   DaemonSets are removed and all 5 MetalLB VIPs still serve traffic.
8. Diagnose the 171-day-failed `truelist-stag-io-cert`.

Item 7 involves a k3s restart — brief API downtime. Items 1-6 do not.

### Phase 1 — Tailscale operator 1.76.6 → 1.102.3

Lowest *blast radius* of the three, and fully independent — but note this is a **26-release
jump**, larger than it first appears. Tailscale ships frequently and maintains backward
compatibility, but the chart's values schema and CRDs have moved over that span, so
diff the rendered output before applying rather than assuming a drop-in upgrade.

No `Connector`/`ProxyClass`/`ProxyGroup` CRs exist; the operator only manages
annotation-driven egress proxies, which limits what can break.

**kubectl access is *not* at risk** — it goes through the node's own `tailscaled`
(`k3s-controller` → 100.90.254.5), not the operator.

Expected impact: the ~25 `ts-*` StatefulSets are recreated, briefly interrupting
cross-cluster Prometheus scraping. `TargetDown` alerts for `cluster="production"`/`"ovh"`
may fire.

### Phase 2 — MetalLB v0.14.5 → v0.16.1

Installed via raw manifests, so upgrade means applying the new upstream manifest set,
not `helm upgrade`. Config is minimal: one `IPAddressPool` (`192.168.20.1-255`) and one
`L2Advertisement`, no BGP.

Verify after: all 5 VIPs still assigned, Traefik still reachable on 192.168.20.1, all
6 Ingresses still resolve.

Note: benign-but-noisy `"Failed to retrieve lbIPs family","reason":"nolbIPsIPFamily"`
errors in the 0.14.x controller log should disappear.

### Phase 3 — cert-manager v1.14.5 → v1.21.2

Gate for all Kubernetes hops. This is a **7-minor jump**; cert-manager supports
upgrading directly between 1.x releases but requires reading the intervening release
notes, and CRDs must be applied **before** the chart.

Consider stepping via an intermediate release (e.g. 1.17) rather than jumping straight
to 1.21, so a failure has a smaller surface to bisect.

Verify after: all 5 Certificates reconcile, `letsencrypt-prod` ClusterIssuer stays
`Ready`, and the webhook is serving (a broken cert-manager webhook blocks all
Certificate/Issuer admission cluster-wide).

### Phase 4 — k3s patch v1.29.4 → v1.29.15+k3s1

No API changes within a patch release, so this validates the upgrade *mechanism*
(binary swap, systemd restart, node drain/uncordon order) at minimal risk.

Order: controller first, then workers one at a time, draining each. Expect workloads
with `local-path` PVCs on the drained node to be unavailable until it returns.

### Phase 5+ — Kubernetes minor hops (DEFERRED)

v1.30 → v1.31 → v1.32 → v1.33 → v1.34 → v1.35 → v1.36, one at a time, each with a
snapshot gate and verification. Reassess after Phase 4.

Before starting: re-sample `apiserver_requested_deprecated_apis` after ≥7 days uptime.

Open question for that phase: continue in-place, or rebuild at v1.36 with corrected
defaults (etcd instead of SQLite, `--disable=servicelb`, possibly HA control plane).
A rebuild avoids 7 hops of deprecation archaeology but requires migrating 38 PVCs,
and Velero covers only 5 namespaces today.

---

## Out of Scope

- Immich deployment (separate spec: `2026-09-12-proxmox-bulk-storage-design.md`)
- Dead-man's switch for `Watchdog` (needs an external heartbeat URL)
- Widening Alertmanager routing to `severity=warning`
- Cleaning up stuck `Terminating` namespaces, duplicate Loki, orphan Kong CRDs
- Node OS upgrade (Ubuntu 24.04 is still supported)
