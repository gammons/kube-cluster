# k3s Homelab Cluster Upgrade — Design

**Date:** 2026-09-12, revised 2026-09-13 after Phase A
**Cluster:** `local-k3s` (kubectl context), 4 VMs on Proxmox host `pve` (192.168.5.1)
**Scope approved:** Phases 0-4, **minus cert-manager**, which was removed from
scope entirely on 2026-09-13 — see "cert-manager: removed from scope". Phase 5+
(Kubernetes minor hops) deferred pending reassessment.

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
| cert-manager | **v1.14.5** (2024-05) | v1.21.2 † | 7 minors | Helm (`jetstack/cert-manager`) |
| Traefik | v2.10.5 (chart 25.0.3) | v3.x | 1 major | k3s packaged component |
| Velero | 1.18.1 | — | — | Helm |

† **There is no cert-manager target. It was removed from scope** on 2026-09-13 and
stays on v1.14.5 — see "cert-manager: removed from scope" below. The gap is real
(1.14 has been EOL since Oct 2024) but it blocks nothing: 1.14 supports Kubernetes
**1.24 → 1.31**.

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

### cert-manager: removed from scope

**cert-manager is not upgraded by any current plan. It stays on v1.14.5.**

This section is the *record*, not a deferred design. It exists because the
expensive part of this work was establishing which previous claims were false,
and that must not have to be re-derived.

#### Why it was removed rather than corrected

The cert-manager reasoning in this project was **confidently wrong three separate
times**, and on each occasion the wrong version was written down as settled — twice
in the reassuring direction, which is the worse one. Three wrong answers recorded as
final is evidence about the process, not about the last answer. Rather than attempt
a fourth correction inside a plan that executes irreversible operations against a
live production cluster, the owner removed the work from scope.

**The next attempt must be derived from the chart source, not from release notes
and not by inference.** Every one of the three errors below came from reading a
summary and reasoning forward from it.

#### Verified facts

All verified 2026-09-13 against primary sources: cert-manager.io/docs/releases,
`helm pull jetstack/cert-manager --version v1.14.5 --untar`, and the v1.17.4
release manifest.

**1. cert-manager 1.14 supports Kubernetes 1.24 → 1.31. It is not a gate.**
The original claim that v1.14.5 "supports Kubernetes ≤1.29" and that its webhooks
"will fail against a 1.30+ API server" was **false**. The installed version would
survive the deferred k8s hops from 1.29 through 1.31 unaided. Nothing in Phases 0-4
depends on moving it.

**2. 1.17 and 1.18 both support Kubernetes 1.29 → 1.33.** The claim that "1.17 is
the newest release supporting 1.29" was **false** — 1.18 covers the identical range.
(1.19 starts at 1.31, so 1.18 is in fact the newest that reaches down to 1.29.)

| cert-manager | Supported Kubernetes | Upstream EOL | LTS |
|---|---|---|---|
| 1.21 | 1.33 → 1.36 | current | — |
| 1.19 | 1.31 → 1.35 | Jul 2026 | — |
| 1.18 | **1.29 → 1.33** | **Mar 2026 — already passed** | none |
| 1.17 | **1.29 → 1.33** | Oct 2025 | commercial (Palo Alto Networks), **Feb 2027** |
| 1.14 | **1.24 → 1.31** | Oct 2024 | — |

1.17's *only* advantage over 1.18 is the commercial LTS to Feb 2027. 1.18 reached
EOL in Mar 2026 with no LTS at all. Both are already past upstream EOL as of the
date of this revision. Choose between them on support lifetime, not on a Kubernetes
range that is identical.

**3. 1.21 supports Kubernetes 1.33 → 1.36** — four minors above this cluster's 1.29,
so it is unusable until after the deferred k8s hops. This part of the earlier
analysis was correct.

**4. The live CRDs are Helm-owned and unprotected.** This is the finding that makes
a careless upgrade destructive, and it is the one that was stated backwards.

Verified from the chart source for **v1.14.5**, the version actually installed:

- The chart has **no `crds/` directory**. (Helm treats `crds/` as unmanaged — never
  templated, never upgraded, never pruned. Its absence is the whole point.)
- The six CRDs are in **`templates/crds.yaml`**, gated on
  `{{- if .Values.installCRDs }}`. They are therefore **ordinary templated
  resources**, part of the release manifest and subject to Helm's pruner.
- There are **zero** occurrences of `resource-policy` anywhere in the 1.14.5 chart
  (`grep -rn resource-policy` over the unpacked chart returns nothing). The chart
  cannot have applied a `keep` annotation, so the six live CRDs carry **no `keep`
  annotation from this source**.

**Consequence:** any upgrade that renders zero CRDs — which is exactly what
`--set crds.enabled=false` does — presents Helm with six resources that were in the
old manifest and are absent from the new one. Helm prunes all six. CRD deletion
cascades to **every Certificate, CertificateRequest, Issuer, ClusterIssuer, Order
and Challenge in the cluster.**

> **One caveat, stated deliberately rather than inferred.** The chart evidence proves
> the *chart* supplies no `keep` annotation and that its CRDs are templated. Whether
> the six live CRDs carry a `keep` annotation from some other source, and whether
> they are in the current release manifest at all, depends on how this cluster was
> actually installed — and that cannot be read from this repo. It must be checked
> against the live cluster before any upgrade:
>
> ```sh
> kubectl --context local-k3s get crd \
>   certificates.cert-manager.io certificaterequests.cert-manager.io \
>   issuers.cert-manager.io clusterissuers.cert-manager.io \
>   orders.acme.cert-manager.io challenges.acme.cert-manager.io \
>   -o jsonpath='{range .items[*]}{.metadata.name}{"\t"}{.metadata.annotations.helm\.sh/resource-policy}{"\n"}{end}'
> helm --kube-context local-k3s get manifest cert-manager -n cert-manager \
>   | grep -c CustomResourceDefinition
> ```
>
> Assuming the answer is exactly the failure mode this whole section exists to
> prevent. The chart facts are settled; the live state is not, and no amount of
> reading the repo will settle it.

**5. The v1.17.4 release manifest carries `helm.sh/resource-policy: keep` on all 6
CRDs.** Verified by fetching `cert-manager.crds.yaml` for v1.17.4: 6
`CustomResourceDefinition` documents, 6 `helm.sh/resource-policy: keep` annotations.
**Applying that file *before* the chart upgrade is what would protect the CRDs.**
That ordering is load-bearing, not bookkeeping. `--set crds.keep=true` is **not**
the protection and cannot be: it only annotates CRDs the chart itself renders, and
with `crds.enabled=false` it renders none, so it is inert.

**6. "The chart never manages them" was false for this release.** That sentence
appeared **twice** in earlier revisions of this spec and plan, both times as the
reassuring version, and both times as the stated reason the CRDs were safe across a
Helm rollback. For chart v1.14.5 the chart *does* template them. Treat any future
statement of that shape as unverified until `ls crds/` and
`grep -rn resource-policy` have been run against the specific chart version in
question.

#### If cert-manager is ever put back in scope

Preconditions, in order:

1. Run the two live-cluster commands in the caveat box above and record the output.
2. `helm pull` both the installed and target chart versions and check `crds/`,
   `templates/crds.yaml` gating, and `resource-policy` in each.
3. Apply the target's `cert-manager.crds.yaml` release manifest **before** the chart
   upgrade, and verify all 6 CRDs carry `keep` **between** the two steps.
4. Only then upgrade the chart.

The cluster must also still be within the target's supported Kubernetes range —
1.21 requires ≥1.33, so it remains unusable until the deferred hops land.

#### Unrelated, and still true

`truelist-staging/truelist-stag-io-cert` has been `READY=False` for **171 days**,
with a `cm-acme-http-solver-sqcfr` pod and orphan solver Ingress still present. This
is a pre-existing failure with nothing to do with any upgrade. Plan Task 7 diagnoses
and records it — worth doing because Task 12 restarts the API server and every node,
and its acceptance test reads `get certificates -A`, where an undocumented long-dead
certificate is indistinguishable from k3s-upgrade fallout.

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

**3. cert-manager's CRD protection is not what it looks like — and the first two
attempts to say what it *is* were both wrong.** The one correct half: with
`crds.enabled=false` the chart renders no CRDs, so `crds.keep=true` has nothing to
annotate and **is inert**. The wrong half, which this entry previously asserted:
that the CRDs are safe because "the chart never manages them". **That is false for
chart v1.14.5** — it has no `crds/` directory and templates all six from
`templates/crds.yaml` under `installCRDs`, making them prunable release members with
no `keep` annotation from the chart. What would actually protect them is applying the
target release's `cert-manager.crds.yaml` (which does carry
`helm.sh/resource-policy: keep` on all 6) **before** the chart upgrade. CRD deletion
cascades to every Certificate, CertificateRequest, Issuer, ClusterIssuer, Order and
Challenge in the cluster. Full record, including what is verified versus what still
needs the live cluster, in "cert-manager: removed from scope" above.

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
3. Establish the snapshot procedure **and test a real rollback** before trusting it.
   **The test is necessarily whole-cluster, not per-node.** An earlier revision said
   to test "on the least critical node"; `snapshot-cluster.sh` has no such mode and
   deliberately never will. A snapshot set is one label across **five** targets — VMs
   100/101/102/103 and `main-pool/k3s-nfs` — and `rollback` stops all four VMs,
   reverts all five, restarts them and restarts `nfs-server`. It is all-or-nothing by
   design, because a partial set leaves the cluster split across two points in time,
   which is worse than no set at all. Plan Task 2 is the real procedure: write a
   canary to both storage layers, roll the whole set back, prove both canaries
   vanish. Budget for the entire cluster being down for the duration.
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

**Watch for a silent monitoring regression.** v0.16.0 replaced kube-rbac-proxy with
native TLS: the metrics port moves **7472 → 9120** and becomes **HTTPS with a
self-signed certificate** (*"The old HTTP endpoints are no longer available"*).
v0.16.1 fixed the resulting scrape failures **in the Helm chart only**, and this
cluster installs from `metallb-native.yaml`, which ships no ServiceMonitor, no
PodMonitor and no `prometheus.io/scheme` annotation. Nothing user-visible breaks —
LB IPs, L2 announcement and Ingresses are unaffected — so this lands as monitoring
that quietly stops reporting, with no alert, on a cluster running
kube-prometheus-stack. Plan Task 9 Step 1 records the "before" and Step 8 checks the
"after"; `metallb/README.md` has the fix. It is deliberately **not** a rollback
trigger — fix it forward.

### Phase 3 — cert-manager: REMOVED FROM SCOPE

**This phase has no work in it.** cert-manager stays on v1.14.5. The phase number is
kept so the Phase 4 / Task 12 numbering and every cross-reference to it still
resolve — the same reason the plan keeps a gap where Tasks 10 and 11 were.

It costs nothing operationally: 1.14 supports Kubernetes **1.24 → 1.31**, so it gates
neither Phase 4's patch bump inside 1.29 nor the first two hops of Phase 5+. It does
leave a real gap — 1.14 has been EOL since Oct 2024 — which is accepted knowingly.

Rationale, the three prior errors, the verified version matrix, and the CRD-pruning
hazard that makes a careless upgrade destructive: see "cert-manager: removed from
scope" under Findings. **Read that before scheduling this work anywhere.**

### Phase 4 — k3s patch v1.29.4 → v1.29.15+k3s1

No API changes within a patch release, so this validates the upgrade *mechanism*
(binary swap, systemd restart, node drain/uncordon order) at minimal risk.

Order: controller first, then workers one at a time, draining each. Expect workloads
with `local-path` PVCs on the drained node to be unavailable until it returns.

### Phase 5+ — Kubernetes minor hops (DEFERRED)

v1.30 → v1.31 → v1.32 → v1.33 → v1.34 → v1.35 → v1.36, one at a time, each with a
snapshot gate and verification. Reassess after Phase 4.

Before starting: re-sample `apiserver_requested_deprecated_apis` after ≥7 days uptime.

**cert-manager constrains this phase even though it is out of scope**, because the
cluster cannot hop past a Kubernetes version the installed cert-manager does not
support. The installed **1.14 supports 1.24 → 1.31**, so:

| Cluster at | Installed cert-manager 1.14 |
|---|---|
| 1.30, 1.31 | **in range** — no action needed |
| 1.32 and above | **out of range** — cert-manager must move first |

So the first two hops can proceed with cert-manager untouched. **The 1.31 → 1.32 hop
is the point at which the cert-manager question has to be reopened**, and reopening
it means working through "cert-manager: removed from scope" under Findings — the
verified matrix, the three prior errors, and the CRD-pruning hazard — rather than
resuming from any earlier plan text.

Note when picking a target at that time: **1.17 and 1.18 support the identical
Kubernetes range (1.29 → 1.33)**, and both are already past upstream EOL. 1.17's only
advantage is a commercial LTS to Feb 2027. 1.21 needs ≥1.33. Whatever is current when
the question is reopened may well be a better answer than any of these — re-derive it
then, from the support matrix and the chart source.

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
- **`cert-manager/install-crd.sh` pins v1.14.5 — what is *installed*, not a target.**
  The pin has moved twice for two different reasons, and the current value is the
  only one that is a statement of fact rather than of intent. It pinned v1.21.2
  (unrunnable here: requires Kubernetes ≥1.33), then v1.17.4 (chosen by reasoning
  since discarded), and now v1.14.5 so that running the script is consistent with
  the deployed state instead of silently performing an unreviewed upgrade. **The
  rule that the pin must never get ahead of the cluster's Kubernetes version still
  holds.** No plan runs this script; it exists for a rebuild. Its header and
  `cert-manager/README.md` both state the deferral and require CRD ownership to be
  verified from the chart source before the pin is raised.
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
