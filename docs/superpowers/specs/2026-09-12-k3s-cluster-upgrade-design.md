# k3s Homelab Cluster Upgrade — Design

**Date:** 2026-09-12, revised 2026-09-13 after Phase A
**Cluster:** `local-k3s` (kubectl context), 4 VMs on Proxmox host `pve` (192.168.5.1)
**Scope approved:** Phases 0-4. Phase 5+ (Kubernetes minor hops, plus the
cert-manager 1.17 → 1.21 move that only becomes possible at Kubernetes 1.33)
deferred pending reassessment.

> **Always pass `--context local-k3s` explicitly.** The kubeconfig contains 8 contexts
> including `aws-truelist-prod` and `production`, and `current-context` has previously
> pointed at a remote production cluster.

## Status: Phase A complete, Phase B not started

Execution was split in two:

| | What | State |
|---|---|---|
| **Phase A** | Author the **file artifacts** — snapshot tool, k3s config, Helm values, docs | **Done.** 21 commits, reviewed, zero cluster mutation |
| **Phase B** | Execute the **live cluster operations** using those artifacts | **Not started** |

The split exists because the review mechanism for Phase A work is `git diff`, and
that mechanism is blind to the thing that actually carries risk in Phase B. Task 6's
diff is a 12-line config file; its *effect* is restarting the API server and deleting
5 DaemonSets. Reviewing the diff verifies almost nothing about the risk.

**If you are picking this up cold:** the executable document is
`../plans/2026-09-13-k3s-cluster-upgrade-phases-0-4.md`. It is current and has been
reconciled against what Phase A actually committed. This spec is the *why*; the plan
is the *how*. Read "Decisions Already Made" at the bottom of this file before changing
anything — several things that look like defects are deliberate, and several things
that look correct are load-bearing for non-obvious reasons.

**These artifacts already exist and are committed. Do not re-author them:**
`proxmox/snapshot-cluster.sh`, `proxmox/README.md`, `k3s/config.yaml`, `k3s/README.md`,
`velero/values.yml`, `tailscale/values.yml`, `tailscale/README.md`, `metallb/README.md`,
`cert-manager/install-crd.sh`, `cert-manager/README.md`.

---

## Current State

All "latest" figures below were verified against upstream repos on 2026-09-12, not
assumed.

| Component | Installed | Latest | Gap | Managed by |
|---|---|---|---|---|
| k3s / Kubernetes | **v1.29.4+k3s1** | v1.36.4+k3s1 | **7 minors** | install script, systemd |
| Tailscale operator | **1.76.6** (2024-11-10) | **1.102.3** | **26 releases** | Helm (`tailscale/tailscale-operator`) |
| MetalLB | **v0.14.5** | **v0.16.1** | 2 minors | **raw `kubectl apply`** (not Helm) |
| cert-manager | **v1.14.5** (2024-05) | **v1.21.2** † | **7 minors** | Helm (`jetstack/cert-manager`) |
| Traefik | v2.10.5 (chart 25.0.3) | v3.x | 1 major | k3s packaged component |
| Velero | 1.18.1 | — | — | Helm |

† **"Latest" is not the target.** cert-manager 1.21 requires Kubernetes ≥1.33 and
cannot run on this cluster at all. The reachable target is **v1.17.4** — see
"cert-manager is EOL, not a gate" below. Closing the remaining 4 minors is Phase 5+
work, gated on the cluster reaching 1.33.

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

Phase A wrapped both into **`proxmox/snapshot-cluster.sh`**, which treats all five
targets as one unit — see `proxmox/README.md`. Use it rather than raw `qm`/`zfs`:
a partial snapshot set is worse than none, because it leaves the cluster split across
two points in time. The script refuses to create a set unless every pool is healthy
and `qemu-guest-agent` answers on all 4 VMs, refuses a label already in use, and
before rolling back verifies the label exists on all five targets, is the *most
recent* snapshot on all five, and that all 4 VMs actually stopped.

**Only the most recent set can be rolled back to, so hold at most one gate at a
time.** Proxmox refuses to roll a zvol back to anything but its newest snapshot
(`PVE/Storage/ZFSPoolPlugin.pm`, `volume_rollback_is_possible`), while
`zfs rollback -r` on the dataset would instead *destroy* the newer snapshots — the
two halves of a "one unit" set fail in opposite directions on the same input. The
script now refuses on both, before stopping anything. Reaching an older set means
deleting the newer ones first, which discards the route back past them: a real
choice, not a formality.

Labels accept `[A-Za-z0-9_-]` only and may not begin with `-`. **Dots are rejected**,
so `pre-k3s-v1.30.5` fails — use `pre-k3s-v1-30-5`.

`main-pool` is at **58% capacity** with **660G available to datasets** (`zpool` reports
766G `FREE`; the 660G `zfs AVAIL` figure is the conservative one to plan against).
Snapshots are cheap — copy-on-write, so cost grows only with divergence during the
upgrade window. Snapshotting all four VMs plus the NFS dataset captures the entire
cluster state atomically and reverts in minutes.

**Most tasks get their own snapshot gate**, which converts a risky migration into a
series of individually reversible steps. Three exceptions, all deliberate:

- **Task 1 cannot be gated.** `snapshot-cluster.sh create` requires the
  `qemu-guest-agent` that Task 1 installs. This is circular and unfixable, so Task 1
  does the minimum (install a package, power-cycle) and Task 2 immediately proves the
  net works. **This is the largest residual risk in Phase B.**
- **Tasks 4, 5 and 7 have no gate and need none.** 4 and 5 are confined to Helm
  releases that `helm rollback` restores; 7 is read-only investigation.
- **Tasks 3, 6 and 12 are the only ones a snapshot is the *sole* route back from.**
  `lvextend` cannot shrink, and k3s does not support in-place downgrade.

**Prerequisite:** VMs have **no `qemu-guest-agent`** (still true — Phase B Task 1
installs it), so snapshots would be crash-consistent only, and two VMs (102/103)
already ignored ACPI shutdown and needed hard stops.

**The rollback procedure must itself be tested before it is depended on** — Phase B
Task 2 writes canary files to both a VM disk and the NFS dataset, rolls back, and
proves both vanish. If either survives, stop the entire plan. `snapshot-cluster.sh`
has **never been run against real infrastructure**; treat Task 1 Step 6 and Task 2 as
tests of the script, not of the cluster.

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

### cert-manager is EOL, not a gate — and it must not get ahead of the cluster

An earlier revision of this spec claimed v1.14.5 "supports Kubernetes ≤1.29" and
that its webhooks "will fail against a 1.30+ API server", making cert-manager a hard
gate that the whole phase ordering hung off. **That was wrong**, and it was wrong in
the direction that produced an unexecutable plan. Checked against
cert-manager.io/docs/releases on 2026-09-13:

| cert-manager | Supported Kubernetes | Upstream EOL |
|---|---|---|
| 1.21 | **1.33 → 1.36** | current |
| 1.17 | **1.29 → 1.33** | Oct 2025 (commercial LTS from Palo Alto Networks to Feb 2027) |
| 1.14 | **1.24 → 1.31** | Oct 2024 |

The installed 1.14.5 supports Kubernetes up to **1.31**, so it gates nothing in
Phases 0-4 and would survive the first two minor hops unaided.

The honest reasons to upgrade it are different, and both still hold:

- **1.14 has been EOL since Oct 2024** — no security or bug fixes.
- **cert-manager must never get *ahead* of the cluster's Kubernetes version.**
  This is the constraint that actually bites, and it bit in the opposite
  direction from the one assumed: the target that was picked, 1.21, requires
  Kubernetes ≥1.33, four minors above this cluster. It cannot be installed here
  at all. The reachable target is **v1.17.4** (newest 1.17 patch, verified
  available), which supports 1.29 and every hop through 1.33.

Because 1.17 tops out at exactly the release 1.21 starts at, **1.33 is the only
Kubernetes version at which the 1.17 → 1.21 move is possible.** That makes
cert-manager a genuine gate — but for the 1.33 → 1.34 hop in Phase 5+, not for
anything in Phases 0-4.

The consequence for the plan is that its cert-manager work splits: Task 10
(→ v1.17.4) is executable now; Task 11 (→ v1.21.2) is not sequenceable until the
Kubernetes hops reach 1.33 and is deferred to Phase 5+.

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

### Defects found during Phase A — do not reintroduce these

Five real defects were caught while authoring the artifacts. Each was verified against
primary sources, not inferred. They are listed because the shape of each is easy to
recreate.

**1. `get.k3s.io` decides server-vs-agent from the environment, not from disk.**
`install.sh:178` is `if [ -z "${K3S_URL}" ]; then CMD_K3S=server`, and
`grep -n "k3s-agent" install.sh` returns **nothing** — the installer has no awareness
of the agent unit. Running a bare `curl … | INSTALL_K3S_VERSION=… sh -` on a worker
installs a **k3s server** beside the untouched agent and swaps the binary underneath
it, so the agent never actually upgrades. `create_env_file()` (`:1031`) also writes
with `tee` and no `-a`, i.e. it truncates and rewrites from the current environment
rather than reading the existing file. **Every agent upgrade must pass `K3S_URL` and
`K3S_TOKEN` explicitly** (token: `/var/lib/rancher/k3s/server/node-token` on the
controller). The correct form was already in this repo's top-level `README.md:25`.

**2. Helm prunes the `operator-oauth` Secret when the Tailscale oauth values are omitted.**
The Secret is release-owned (`app.kubernetes.io/managed-by: Helm`) and the chart gates
it on `.Values.oauth.clientId`. Rendering with oauth unset drops it from the manifest,
and Helm deletes resources that disappear between manifests. `tailscale/values.yml`
deliberately omits the credential, so **the Secret must be annotated
`helm.sh/resource-policy=keep` before the first upgrade with those values** or the
credential is destroyed — including a freshly rotated one.

**3. cert-manager's CRD protection is not what it looks like.** With
`crds.enabled=false` the chart renders no CRDs, so `crds.keep=true` has nothing to
annotate — **it is inert.** What actually protects them is that the chart never manages
them, plus the released `cert-manager.crds.yaml` carrying `helm.sh/resource-policy: keep`
on all 6 CRDs. Verify the annotation is present before upgrading; CRD deletion cascades
to every Certificate, Issuer, Order and Challenge in the cluster.

**4. Piping *and* redirecting into the same `ssh` is zsh-only.** Under `bash`/`sh` the
redirect wins and the piped sudo password is silently discarded. Combined with a
`2>/dev/null` this fails **silently**. Feed both down one stream instead:
`{ printf '%s\n' "$PW"; cat file; } | ssh … 'sudo -S -p "" tee …'`.

**5. `cmd || echo "<success-sounding message>"` hides failures.** A command whose
failure is indistinguishable from the desired outcome will self-certify. This bit the
rollback proof: if the canaries were never written, the post-rollback check prints
"canary gone" and passes. Gate such checks behind an explicit liveness assertion that
hard-fails.

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

### Phase 3 — cert-manager v1.14.5 → v1.17.4

**Not** a gate for the Kubernetes hops in this scope — 1.14 supports Kubernetes up
to 1.31 and would survive them. The reason to move is that 1.14 has been EOL since
Oct 2024.

v1.17.4 is the **ceiling**, not a waypoint: 1.17 is the newest cert-manager that
supports Kubernetes 1.29, and 1.21 requires ≥1.33. This is a 3-minor jump;
cert-manager supports upgrading directly between 1.x releases but requires reading
the intervening release notes, and CRDs must be applied **before** the chart.

**The trade being made, deliberately:** 1.17 reached upstream EOL in Oct 2025, so
this lands on a release that gets no upstream fixes. Palo Alto Networks publish a
commercial **1.17 LTS supported to Feb 2027**, which sets the horizon. Stopping here
is a decision with a deadline attached, not an oversight: the cluster must reach
Kubernetes 1.33 — and cert-manager 1.21 with it — before Feb 2027, or accept running
unmaintained certificate infrastructure. That is the real schedule pressure behind
Phase 5+, and it is a stronger argument for doing the hops than anything in the
original "cert-manager is a hard gate" framing.

Verify after: all 5 Certificates reconcile, `letsencrypt-prod` ClusterIssuer stays
`Ready`, and the webhook is serving (a broken cert-manager webhook blocks all
Certificate/Issuer admission cluster-wide).

### Phase 4 — k3s patch v1.29.4 → v1.29.15+k3s1

No API changes within a patch release, so this validates the upgrade *mechanism*
(binary swap, systemd restart, node drain/uncordon order) at minimal risk.

Order: controller first, then workers one at a time, draining each. Expect workloads
with `local-path` PVCs on the drained node to be unavailable until it returns.

### Phase 5+ — Kubernetes minor hops, and the cert-manager 1.21 move (DEFERRED)

v1.30 → v1.31 → v1.32 → v1.33 → v1.34 → v1.35 → v1.36, one at a time, each with a
snapshot gate and verification. Reassess after Phase 4.

Before starting: re-sample `apiserver_requested_deprecated_apis` after ≥7 days uptime.

**cert-manager 1.17 → 1.21 belongs in the middle of this phase, at exactly one
point.** 1.17 supports Kubernetes up to 1.33; 1.21 requires ≥1.33. So:

| Cluster at | cert-manager must be |
|---|---|
| 1.29 → 1.31 | 1.14 (supported) or 1.17 |
| 1.32 → 1.33 | **1.17** (1.14 is out of range above 1.31) |
| 1.34 → 1.36 | **1.21** (1.17 is out of range above 1.33) |

Kubernetes **1.33 is the only version both support**, so the 1.17 → 1.21 upgrade
must happen while the cluster is sitting on 1.33 — after that hop lands and before
the 1.33 → 1.34 hop starts. Missing that window means going back down, not forward.

The plan's **Task 11** is this upgrade, written and reviewed but not sequenceable in
Phases 0-4. It is deferred here rather than deleted; see the "DEFERRED" marker in its
slot in `../plans/2026-09-13-k3s-cluster-upgrade-phases-0-4.md`. Give it its own
snapshot gate, taken and released within the task, exactly as Task 10 does.

Deadline: the 1.17 commercial LTS ends **Feb 2027**. Reaching 1.33 is what unblocks
getting off it.

Open question for that phase: continue in-place, or rebuild at v1.36 with corrected
defaults (etcd instead of SQLite, `--disable=servicelb`, possibly HA control plane).
A rebuild avoids 7 hops of deprecation archaeology but requires migrating 38 PVCs.
Velero's coverage is the wrong thing to lean on either way: after Phase B Task 4 it
spans ~25 namespaces (6 before), but **namespaced objects only** —
`includeClusterResources` is unset, so PVs, StorageClasses, CRDs and ClusterIssuers
are not in it. A rebuild has to plan the PV migration explicitly rather than treating
Velero as the mechanism.

---

## Decisions Already Made

Distilled from the Phase A execution ledger. Read this before changing anything —
several things that look like defects are deliberate, and several that look
incidental are load-bearing.

### Deliberately left as-is — do not "fix" these

| Thing | Why it stays |
|---|---|
| `snapshot-cluster.sh create` is non-atomic on mid-loop failure | Fails loudly; the *next* `create` names the stale targets and directs you to `delete`. Bounded. |
| `rollback` surveys all five targets *before* the confirm prompt | Leaves a human-time TOCTOU window, accepted deliberately: never ask an operator to confirm an operation that cannot complete. Single operator, homelab. |
| Both `create`-path ABORTs write to stdout, other aborts use `>&2` | Preserves original behaviour; Task 0 Step 4's expectation reads from stdout. Normalise stream hygiene across the whole script or not at all. |
| `sleep 45` after starting the control plane instead of polling | The Proxmox host has no kubectl; node readiness is verified separately downstream with a real check. |
| `crds.enabled=false` / `crds.keep=true` although both are already chart defaults | Documents intent and survives a future default change. |
| `K3S_TOKEN=` passed on the installer command line (visible in the worker's `ps` argv) | The token already lives on that node in `k3s-agent.service.env`; exposure is seconds. The alternative is an untested `sudo -S` stdin construct in a production upgrade step — the larger risk. |
| `velero/values.yml` omits `includeClusterResources` | A non-empty `excludedNamespaces` keeps Velero's auto-rule false regardless, so CRDs/ClusterRoles/StorageClasses/ClusterIssuers stay unbacked-up. **No regression** — identical to the previous allow-list — but "coverage is the default" is not yet true cluster-wide. Setting it to `true` is a separate decision needing a version check. |

### Load-bearing for non-obvious reasons — do not simplify

- **`local id snaps` declared separately from `snaps=$(...)`** in `snapshot-cluster.sh`.
  `local s=$(false)` returns status 0; the split form returns 1. Inline this and every
  SSH failure is silently swallowed, making an unreachable target look like "label
  absent" — which would defeat the pre-flight entirely.
- **Dash-only snapshot labels.** The charset check rejects dots, so every label in the
  plan is dash-separated on purpose, not stylistically.
- **`cert-manager/install-crd.sh` pins v1.17.4, not the newest cert-manager.** The
  pin is bounded by the *cluster's* Kubernetes version. It previously pinned v1.21.2,
  which requires Kubernetes ≥1.33 — the repo's own install script would have
  installed a version this cluster cannot run. Raise it only alongside Kubernetes,
  and never ahead of it. Task 10 still uses explicit inline commands rather than
  running the script, so the plan and the script stay independently reviewable.
- **Two tasks in the plan write repo files: Task 6 (Step 9, top-level `README.md`)
  and Task 7 (Step 4, `cert-manager/README.md`).** An earlier revision of this spec
  said Task 7 was the only one; that claim was added one commit *after* Task 6 Step 9
  was, and was simply stale. Both tasks' `Files:` lines now say so; every other task's
  reads `none`.

### Open questions — for the human, not the executing agent

- **Velero backups now carry Secrets into S3. Who can read them, and who holds the
  key?** Widening to ~25 namespaces sweeps cert-manager's ACME account key and every
  TLS private key, `infra` registry credentials, `truelist-staging` DB credentials,
  and the Tailscale OAuth secret into `s3://gammons-velero-homelab`.

  **The objects are not unencrypted.** An earlier revision of this question said the
  bucket had "no `serverSideEncryption`, no `kmsKeyId`", reading only
  `backupStorageLocation[0].config` in `velero/values.yml`, which does set just
  `region`. But `velero/README.md:38-39` records `put-bucket-encryption` with
  `SSEAlgorithm: AES256` applied at the **bucket** level, which encrypts every object
  regardless of what the BSL asks for. The BSL config being bare is not evidence of
  plaintext.

  What is genuinely unresolved is narrower, and still a human decision:

  - **Key management.** Bucket-default SSE-S3 (AES256) means AWS holds the key and
    any principal with `s3:GetObject` gets plaintext back transparently. SSE-KMS with
    a customer-managed key would make `kms:Decrypt` a second, separately auditable
    gate, and would let the key be disabled. Worth it for this content, or not?
  - **Bucket access policy.** Public access is blocked and the `velero-homelab` IAM
    user is scoped to this bucket, but nothing has been checked about which *other*
    principals in the account can read it. With cluster-wide Secrets in there, that
    set is the real blast radius.

  **This must be answered before Task 4 Step 5**, which *is* the first widened backup.
- **That first widened backup will likely exceed Task 4's own 60-minute rollback
  trigger.** Newly in scope: harbor ~107Gi, tv-channel 75Gi, monitoring's 50Gi
  Prometheus TSDB, elk 20G. Filesystem-backing a live TSDB or Elasticsearch data
  directory is also unlikely to restore cleanly. Prefer excluding those *volumes*
  (`backup.velero.io/backup-volumes-excludes`) over excluding the namespaces.
- **The Tailscale OAuth client secret was exposed in a terminal session on 2026-09-13**
  and must be rotated. Task 8 Step 2 performs the rotation.

### Known-broken, pre-existing, out of scope

- `truelist-staging/truelist-stag-io-cert` has been `READY=False` for 171 days with a
  stuck ACME solver. Task 7 diagnoses and documents it; fixing it is a separate
  decision (likely DNS-01, or deleting the Certificate).
- `signoz` and `longhorn-system` have been `Terminating` for 2y+. Excluded from Velero
  for that reason. Leftover Longhorn CRDs also shadow the short name `backup`, so
  **always fully-qualify `backups.velero.io`**.
- `~/sudo-pw.txt` is a plaintext password on disk, used throughout the plan.

---

## Out of Scope

- Immich deployment (separate spec: `2026-09-12-proxmox-bulk-storage-design.md`)
- Dead-man's switch for `Watchdog` (needs an external heartbeat URL)
- Widening Alertmanager routing to `severity=warning`
- Cleaning up stuck `Terminating` namespaces, duplicate Loki, orphan Kong CRDs
- Node OS upgrade (Ubuntu 24.04 is still supported)
