# k3s Cluster Upgrade — Phases 0-4 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the homelab cluster safe to upgrade (tested rollback + fix pre-existing breakage), then bring the Tailscale operator and MetalLB current and validate the k3s upgrade mechanism with a patch-level bump. **cert-manager is explicitly out of scope** — see "Task numbering: 10 and 11 do not exist" below.

**Architecture:** Every change that cannot be undone in place is gated behind a *snapshot set* — a Proxmox VM snapshot of all 4 VMs plus a ZFS snapshot of `main-pool/k3s-nfs`, created and rolled back as one unit, which together capture the entire cluster state. Work proceeds lowest-risk first so the rollback mechanism is proven before anything important depends on it. Kubernetes minor version hops (1.30 → 1.36) are explicitly **out of scope** and deferred to a later plan.

**Which tasks are gated, and which are deliberately not.** "Every change is gated" was the original claim and it was not true; state it precisely instead, because a task that believes it has a gate it does not have is worse than one that knows it has none.

| Task | Gate | Why |
|---|---|---|
| 3, 6, 8, 9, 12 | **yes** — `create` at the start, `delete` after verification passes | irreversible or cluster-wide: LV extension, k3s config + restart, operator/manifest upgrades, k3s binary swap |
| 0, 7 | no | read-only; Task 7's only write is a commit |
| 1 | **not possible** | Task 1 installs the `qemu-guest-agent` that `snapshot-cluster.sh create` *refuses to run without*. Its rollback trigger says so and routes to forward diagnosis. |
| 2 | n/a | Task 2 **is** the rollback test; its own snapshot is the subject, and Step 6 deletes it |
| 4, 5 | no — `helm rollback` instead | both change only Helm release state, which is reversible from Helm's own history. Each records the current revision in Step 1 so the rollback target is a fact rather than a guess. |
| 10, 11 | **removed from scope** | both were cert-manager upgrades. Deleted, not deferred-in-place — see "Task numbering: 10 and 11 do not exist" below. |

### Task numbering: 10 and 11 do not exist

**The gap between Task 9 and Task 12 is deliberate.** Tasks 10 and 11 were the
cert-manager upgrades (→ v1.17.4 and → v1.21.2). Both have been removed from this
plan entirely. Task 12 keeps its number: roughly 30 cross-references in this file
and in the spec point at it by number, another session is editing this repo, and a
renumber would churn all of them for no benefit. **If you are reading this and
wondering whether a task was lost in an edit — no. Go from Task 9 straight to
Task 12.**

The reasons for removal, and what a future attempt must do first, are recorded in
`docs/superpowers/specs/2026-09-12-k3s-cluster-upgrade-design.md` under
"cert-manager: removed from scope". Do not reconstruct the upgrade from this
plan's history; the reasoning in those deleted tasks was wrong.

**Exactly one gate may be live at a time.** Every gate is created and deleted inside
a single task, so **no task may assume a gate taken by an earlier one is still
there** — by the time it starts, there is nothing left to roll back to.

This is not a stylistic preference, it is what the tool can actually do. Proxmox
refuses to roll a zvol back to anything but its **most recent** snapshot
(`PVE/Storage/ZFSPoolPlugin.pm`, `volume_rollback_is_possible`), so a second, newer
gate does not add a second route back — **it takes the older one away.** The
`main-pool/k3s-nfs` half does not even fail the same way: `zfs rollback -r` would
succeed there by *destroying* the newer snapshot, so the "one unit" set would come
apart in exactly this case. `snapshot-cluster.sh rollback` now checks recency on all
five targets before it stops anything and refuses if the label is not newest.

**That check is narrower than Proxmox's, and the difference can still cost the
cluster.** It reads `qm listsnapshot` — VM *config* snapshots — while Proxmox reads
every snapshot on the zvol. A `vzdump` leftover, a replication snapshot or a manual
`zfs snapshot` is invisible to the pre-flight and blocking to Proxmox, which puts
you back in exactly the failure the check was written to prevent. **Task 0 Step 2c
is the mitigation and is not optional.** Details in `proxmox/README.md`, "What this
guard does not cover".

An earlier revision of this plan claimed the opposite: that the two cert-manager
tasks (then 10 and 11) could leave "two gates live… That is intentional", with the
older one still "available as a route back". **It was not available** — with a newer
gate present, a rollback to the older one would have stopped all 4 VMs and then died
on the first `qm rollback`, leaving the cluster powered off with nothing reverted.
Both tasks have since been removed from scope, so the situation no longer arises
here — but the rule survives them, because it is a property of Proxmox, not of those
tasks. Every remaining gated task takes and releases its gate within itself.

**Tech Stack:** Proxmox VE 8.4.11 + ZFS, k3s v1.29.4 (SQLite datastore, single server), Helm 3, Ubuntu 24.04 guests, kube-prometheus-stack, Velero 1.18.1.

**Spec:** `docs/superpowers/specs/2026-09-12-k3s-cluster-upgrade-design.md`

---

## Global Constraints

- **Always pass `--context local-k3s` to `kubectl` and `--kube-context local-k3s` to `helm`.** The kubeconfig holds 8 contexts including `aws-truelist-prod` and `production`; `current-context` has previously pointed at a remote production cluster.
- **Always fully-qualify `backups.velero.io`.** Leftover Longhorn CRDs from the 2y-`Terminating` `longhorn-system` namespace shadow the short name `backup`.
- **Never echo the sudo password.** Read it from `~/sudo-pw.txt` into a variable and pipe it. The working pattern, verified on all 4 nodes:
  ```sh
  PW=$(tr -d '\n' < ~/sudo-pw.txt)
  printf '%s\n' "$PW" | ssh -o BatchMode=yes grant@<host> 'sudo -S -p "" <command>'
  ```
- **Proxmox host** is `root@192.168.5.1` (key-based SSH, no sudo needed).
- **Nodes** are reachable by Tailscale hostname as user `grant`: `k3s-controller`, `k3s-node-1`, `k3s-node-2`, `k3s-node-3`.
- **Pin every Helm `--version`.** An unpinned upgrade silently moves the chart too.
- **Secrets must never enter git.** Two are currently stored inline in Helm values: the Slack webhook (already migrated to a mounted Secret) and the **Tailscale OAuth `clientSecret`** (Task 8 handles it). Before committing any values file, run the secret sweep in Task 0.
- **This is infrastructure, not application code, so TDD is adapted:** each task defines its verification command **first**, runs it to confirm it currently reports the *pre-change* state, applies the change, then re-runs the same command to confirm the *post-change* state. Never apply a change before knowing what "before" looks like.
- **Every task has an explicit rollback trigger.** If the trigger fires, stop and roll back rather than attempting a forward fix.

### Current versions (verified 2026-09-12)

| Component | Installed | Target this plan |
|---|---|---|
| k3s | v1.29.4+k3s1 | **v1.29.15+k3s1** (patch only) |
| Tailscale operator | 1.76.6 | 1.102.3 |
| MetalLB | v0.14.5 | v0.16.1 |
| cert-manager | v1.14.5 | **no change — out of scope** |

**cert-manager is not upgraded by this plan.** It stays on the installed v1.14.5.
This is not a deferral of a blocked step: cert-manager 1.14 supports Kubernetes
**1.24 → 1.31**, so it blocks nothing in Phases 0-4 and would survive the Task 12
patch bump and the first two minor hops unaided. The reasons for removing it, and
the preconditions for any future attempt, are in the spec under "cert-manager:
removed from scope". Do not add a cert-manager step back into this plan without
reading that section first.

---

## File Structure

> **All of these files are already written and committed** (Phase A). This plan's
> remaining work is the live cluster operations. Where a task previously said
> "create/modify X", it now says "verify the committed X" and points at the
> commit. **Do not re-author any of them from this document** — the repo is the
> source of truth, and a copy here would drift. There are two exceptions, and
> only two: **Task 7 Step 4** appends a "Known issues" section to
> `cert-manager/README.md` using values that can only be read from the live
> cluster, and **Task 6 Step 9** corrects two statements in the top-level
> `README.md` that Task 6 Step 4 makes false. Both commit their own file.

| File | Responsibility |
|---|---|
| `proxmox/snapshot-cluster.sh` | Create/list/rollback/delete the snapshot set (4 VMs + NFS dataset) as one unit |
| `proxmox/README.md` | "Cluster snapshots" section documenting the script |
| `k3s/config.yaml` | Declarative k3s server config (`disable: servicelb`, persistent control-plane taint) |
| `k3s/README.md` | How k3s is configured and upgraded |
| `velero/values.yml` | Backup schedule, deny-list (`excludedNamespaces`) rather than allow-list |
| `tailscale/values.yml` | Tailscale operator values, secret-free |
| `tailscale/README.md` | Install/upgrade, why the OAuth secret is not in git, and the `helm.sh/resource-policy=keep` requirement |
| `metallb/README.md` | Manifest URL (`v0.16.1`) and config filename |
| `cert-manager/install-crd.sh` | **Not used by this plan.** Pinned to **v1.14.5** to match what is deployed, since the upgrade is out of scope. Its header states the deferral and the CRD-ownership check any future re-pin must do first. |
| `cert-manager/README.md` | **Not used by this plan**, except that Task 7 Step 4 appends a "Known issues" section to it. Records the deferral and the same warning. |

---

## Task 0: Shared helpers and preflight

**Files:** none — `proxmox/snapshot-cluster.sh` was authored, reviewed and
committed in Phase A. This task verifies it; it does not write it.

**Interfaces:**
- Produces: `snapshot-cluster.sh {create|list|rollback|delete} <label>` — used as the gate by every later task.

- [ ] **Step 1: Confirm sudo works on all 4 nodes**

```sh
PW=$(tr -d '\n' < ~/sudo-pw.txt)
for h in k3s-controller k3s-node-1 k3s-node-2 k3s-node-3; do
  printf "%-16s " "$h:"
  printf '%s\n' "$PW" | ssh -o BatchMode=yes -o StrictHostKeyChecking=accept-new grant@$h \
    'sudo -S -p "" id -u' 2>/dev/null
done
```

Expected: `0` for all four hosts. If any is not `0`, **stop** — every later task needs root.

- [ ] **Step 2: Check the two host-side conditions every gate depends on**

All three of these are properties of the Proxmox host, not of the cluster, and each
silently decides whether the gates in Tasks 3, 6, 8, 9 and 12 will work at all.
Check them now, while nothing has changed, rather than discovering them at the
first `create`.

**2a — pool health.**

```sh
ssh root@192.168.5.1 'zpool status -x'
```

Expected: `all pools are healthy`. `snapshot-cluster.sh create` aborts unless
that exact string is present, and its check is **pool-agnostic** — it does not
care which pool is degraded. Without this step the first failure would surface at
Task 1 Step 6, after the guest-agent install and four VM power-cycles have
already happened. Decide here:

- **`main-pool` is unhealthy → stop the plan.** It holds the VM zvols *and*
  `k3s-nfs`, so there is no gate and no rollback to be had. Fix the pool first.
- **`bulk-pool` is unhealthy → still stop, but for a different reason.**
  `bulk-pool` holds no k3s VM state, so it is not a risk to anything in this
  plan; it is a risk to *executing* this plan, because `create` will refuse
  while any pool is degraded. Resolve or clear the condition (`zpool status
  bulk-pool` for the cause; `zpool clear bulk-pool` if it is a latched
  historical error) and re-run this step. **Do not edit the script to check only
  `main-pool` as a way past this.** Narrowing the gate's own guard mid-plan is a
  deliberate change to the safety mechanism and belongs in its own review, not
  in an unblocking step.

**2b — the NFS dataset must have no child datasets.**

```sh
ssh root@192.168.5.1 'zfs list -r -o name main-pool/k3s-nfs'
```

Expected: exactly one data line, `main-pool/k3s-nfs`. If **any** child dataset is
listed, **stop and reassess before using any gate.** The script's header claims
the snapshot set covers all 15 nfs PVCs, and with children present it does not:
`create` runs `zfs snapshot` **non-recursively**, and the `-r` in `zfs rollback
-r` means "destroy snapshots newer than this one", not "recurse into children".
Any child dataset is therefore outside the safety net while appearing to be
inside it. (`dataset_snapshots()` already filters `^main-pool/k3s-nfs@` and says
in a comment that child snapshots are ignored, so the code anticipates children
even though `create` never makes them.)

**Do not fix this by switching `create` to `zfs snapshot -r`.** Recursive
snapshots change what a rollback means and which PVCs move together, and that is
a decision about the safety mechanism — it needs its own review, not an in-flight
edit.

**2c — no snapshot the recency guard cannot see, and nothing scheduled to make one.**

`snapshot-cluster.sh`'s recency check reads `qm listsnapshot`, the VM **config**
snapshot view. Proxmox's own `volume_rollback_is_possible()` reads `zfs list -t
snapshot -r <zvol>` and considers **every** snapshot on the zvol regardless of
origin. A `vzdump` leftover, a storage-replication snapshot (`__replicate_*`) or a
manual `zfs snapshot` is therefore **invisible to the pre-flight and blocking to
Proxmox** — the guard passes, all 4 VMs stop, the first `qm rollback` dies, and the
cluster is powered off with nothing reverted. That is the total-outage failure the
guard exists to prevent, reached by a route it does not watch. It is documented,
not fixed; see "What this guard does not cover" in `proxmox/README.md`. **This step
is the mitigation, so do not skip it** — it is the only thing covering that gap.

First, list every zvol snapshot on the four VM disks:

```sh
ssh root@192.168.5.1 'for id in 100 101 102 103; do
  qm config "$id" \
    | sed -n "s/^\(scsi\|virtio\|sata\|ide\)[0-9]\+: \([^,]*\).*/\2/p" \
    | while read -r volid; do
        path=$(pvesm path "$volid" 2>/dev/null) || continue
        case "$path" in
          /dev/zvol/*) zfs list -t snapshot -H -o name -r "${path#/dev/zvol/}" ;;
          *) echo "NOT-A-ZVOL $volid -> $path" ;;
        esac
      done
done' | sort
./proxmox/snapshot-cluster.sh list
```

Expected: the first command lists **only** snapshots that also appear in
`snapshot-cluster.sh list` (plus `NOT-A-ZVOL` lines for cdrom/`none` entries,
which are informational). Anything in the first and not the second is a snapshot
the tool cannot see and Proxmox will honour — **stop and resolve it before taking
any gate.**

**If the first command prints nothing at all, do not read that as "clean."**
Nothing in this repo has been run against the host, and these disk keys and
storage IDs come from documentation rather than from this machine. Empty output is
much more likely to be a parsing miss than four VMs with no snapshots. Verify by
hand — `ssh root@192.168.5.1 'qm config 100'` and read what the disk lines
actually look like — and fix the `sed` before trusting the result.

Second, confirm nothing is scheduled to create one *while* a gate is held. A gate
can be clean when taken and blocked an hour later by a backup that started in
between:

```sh
ssh root@192.168.5.1 'echo "== backup jobs =="; cat /etc/pve/jobs.cfg 2>/dev/null; \
  cat /etc/pve/vzdump.cron 2>/dev/null; \
  echo "== replication =="; pvesr status 2>/dev/null; \
  cat /etc/pve/replication.cfg 2>/dev/null'
```

Expected: **no backup job and no replication job selecting VMs 100-103** (watch for
`all: 1` / `exclude:` forms as well as explicit `vmid:` lists — an all-VMs job
covers these four without naming them). If one exists, decide before starting:
disable it for the duration of the plan, or accept that any gate may be
un-rollbackable at the moment you need it. **Do not leave this to chance and do not
discover it mid-incident.** If you disable a job, write down that you did — it has
to go back on afterwards.

- [ ] **Step 3: Record the baseline everything is compared against**

**These files must outlive `/tmp`.** Tasks 2, 6, 9 and 12 diff against
`baseline-lb.txt`, and they may run days later, across four VM power-cycles
(Task 1), a full cluster rollback (Task 2) and any number of workstation
reboots. A `tmpfs` `/tmp`, or `systemd-tmpfiles` ageing, silently removes the
one artifact the acceptance tests of three separate tasks are measured against.
Put them in the SDD workspace instead, which is gitignored (`.superpowers/sdd/.gitignore`
is `*`) so nothing here can reach a commit:

```sh
BASE=.superpowers/sdd/2026-09-13-k3s-cluster-upgrade-phases-0-4/baseline
mkdir -p "$BASE"
kubectl --context local-k3s get nodes -o wide > "$BASE/nodes.txt"
kubectl --context local-k3s get pods -A --field-selector=status.phase!=Succeeded > "$BASE/pods.txt"
kubectl --context local-k3s get svc -A -o json | jq -r '.items[]|select(.spec.type=="LoadBalancer")|"\(.metadata.namespace)/\(.metadata.name) \((.status.loadBalancer.ingress//[])|map(.ip)|join(","))"' | sort > "$BASE/lb.txt"
kubectl --context local-k3s get ingress -A > "$BASE/ingress.txt"
kubectl --context local-k3s get certificates -A > "$BASE/certs.txt"
wc -l "$BASE"/*.txt
git status --porcelain -- .superpowers/   # must print nothing
```

Expected: 5 non-empty files, and no `git status` output. `lb.txt` must contain
exactly these 5 lines (sorted):
```
infra/registry 192.168.20.50
kube-system/traefik 192.168.20.1
unifi/lb-unifi 192.168.20.10
unifi/lb-unifi-udp 192.168.20.4
wprb-rocks/wprb-rocks-backend-service 192.168.20.2
```

`$BASE` is referenced by every later task that diffs against it. Set it again at
the top of any shell where you use it — it is a shell variable, not a file:

```sh
BASE=.superpowers/sdd/2026-09-13-k3s-cluster-upgrade-phases-0-4/baseline
```

**If `$BASE/lb.txt` is missing when a later task needs it, regenerate it — do not
skip the diff and do not hand-write the file.** Re-run the `lb.txt` line above and
check the result against the 5 lines printed here. A regenerated baseline is only
trustworthy if it still matches them; if it does not, the drift it would have
caught has already happened, and *that* is the finding. Say so and stop rather
than adopting the new output as the baseline.

- [ ] **Step 4: Verify the committed snapshot script**

`proxmox/snapshot-cluster.sh` was authored and reviewed in Phase A — `16bfb36`
(initial), `f8afe22` (whole-set pre-flight), `3a11cfe` (verify VMs stopped before
rollback), `7016922` (pool-health guard no longer races a pipeline), `5fd3a6c`
(wall-clock stop bound, `ConnectTimeout`, partial-set reporting), `f0dc943`
(refuse a rollback that is not to the most recent snapshot). **Do not
rewrite it.** Read it, then confirm it is present, executable and unmodified:

```sh
test -x proxmox/snapshot-cluster.sh && bash -n proxmox/snapshot-cluster.sh && echo "script OK"
git status --porcelain -- proxmox/snapshot-cluster.sh
git log --oneline -1 -- proxmox/snapshot-cluster.sh
```

Expected: `script OK`, no output from `git status` (clean), and the most recent
commit touching it is `f0dc943` or a later reviewed fix — not a local edit. The
commit list above is provenance, not a checksum: what matters is that `git
status` is clean and that the tip is one of these or a reviewed successor.

Read the file before using it. The behaviour every later task depends on:
`create` aborts unless all pools are healthy, `qemu-guest-agent` answers on all
4 VMs, and the label is unused on all 5 targets; `rollback` aborts unless the
label is present on all 5 targets, is the **most recent** snapshot on all 5, and
every VM has actually stopped within 60s of wall-clock; `delete` is idempotent
but fails loudly if a target refuses to give the snapshot up. `create` and
`rollback` both report the *exact* partial state if they fail part way through —
read that report rather than re-running blindly.

**The recency check is why this plan holds one gate at a time.** Proxmox will not
roll a zvol back to anything but its newest snapshot, so a newer gate does not add
a route back, it removes the older one. If a rollback aborts naming a blocking
label, deleting that label is the only way past — and it throws away the route back
past *it*. Decide that deliberately; do not do it reflexively mid-incident.

**Know its blind spot before you depend on it.** The check reads `qm listsnapshot`
and therefore sees only VM *config* snapshots; Proxmox enforces against every
snapshot on the zvol. A rollback can abort naming a label this script never showed
you. Read "Known limitation 2" above `vm_blocking_snapshots()` in the script and
"What this guard does not cover" in `proxmox/README.md`, and run Task 0 Step 2c.

- [ ] **Step 5: Confirm it refuses to run (agent not installed yet)**

```sh
./proxmox/snapshot-cluster.sh create preflight-check
```

Expected: `ABORT: qemu-guest-agent not responding on VM 100`.
This is the correct pre-change state — the guard works, and Task 1 installs the agent.

- [ ] **Step 6: Nothing to commit**

The script is already committed (Phase A). Confirm the working tree is clean for
it rather than attempting a commit — `git commit` with nothing staged exits
non-zero and would fail the task:

```sh
git status --porcelain -- proxmox/snapshot-cluster.sh
```

Expected: no output. If there *is* output, something edited the script locally —
investigate before proceeding; every later gate depends on it.

**Rollback trigger:** none — nothing changed yet.

---

## Task 1: Install qemu-guest-agent on all 4 VMs

Without it, Proxmox cannot quiesce guest filesystems, so snapshots are crash-consistent only. VMs 102 and 103 already ignored ACPI shutdown once and required hard stops.

**Files:** none in repo (host + guest config)

**Interfaces:**
- Consumes: sudo access from Task 0.
- Produces: `qm agent <id> ping` responds on all 4 VMs — required by `snapshot-cluster.sh create`.

- [ ] **Step 1: Verify the agent is currently absent (pre-change state)**

```sh
for id in 100 101 102 103; do printf "VM %s: " $id; ssh root@192.168.5.1 "qm agent $id ping" 2>&1 | head -1; done
```

Expected: `No QEMU guest agent configured` for all four.

- [ ] **Step 2: Install the agent inside each guest**

```sh
PW=$(tr -d '\n' < ~/sudo-pw.txt)
for h in k3s-controller k3s-node-1 k3s-node-2 k3s-node-3; do
  echo "== $h"
  printf '%s\n' "$PW" | ssh -o BatchMode=yes grant@$h \
    'sudo -S -p "" sh -c "DEBIAN_FRONTEND=noninteractive apt-get install -y qemu-guest-agent >/dev/null 2>&1 && systemctl enable --now qemu-guest-agent && systemctl is-active qemu-guest-agent"'
done
```

Expected: `active` for each host.

- [ ] **Step 3: Enable the agent device on each VM**

The guest daemon cannot talk to the host until the virtio-serial device exists. This requires a VM power cycle.

```sh
for id in 100 101 102 103; do ssh root@192.168.5.1 "qm set $id --agent enabled=1"; done
```

Expected: `update VM <id>: -agent enabled=1` for each.

- [ ] **Step 4: Define the power-cycle helper (does not cycle anything yet)**

A reboot from inside the guest is **not** enough — the virtio-serial device is attached at VM start. Do the workers first and the controller last so the API stays up as long as possible.

**The VMID→hostname mapping is not sequential.** Verified via MAC address against the ARP table and each node's `INTERNAL-IP`:

| VMID | Proxmox name | Hostname | IP |
|---|---|---|---|
| 100 | k3s-controller | `k3s-controller` | 192.168.10.1 |
| 101 | k3s-worker-2 | `k3s-node-2` | 192.168.10.3 |
| 102 | k3s-worker-3 | `k3s-node-3` | 192.168.10.4 |
| 103 | k3s-worker-1 | `k3s-node-1` | 192.168.10.2 |

**This task has no snapshot gate and cannot have one** (Step 6 is the first moment
one is possible), so it is the single worst place in the plan to lose more than one
node at a time. Steps 4a-4d below cycle exactly one VM each, with a readiness check
between them that you must read before starting the next. Define the helper first
and cycle nothing:

```sh
PW=$(tr -d '\n' < ~/sudo-pw.txt)
cycle_vm() {   # $1 = vmid, $2 = hostname
  echo "== cycling VM $1 ($2)"
  printf '%s\n' "$PW" | ssh -o BatchMode=yes grant@"$2" 'sudo -S -p "" shutdown -h now'
  echo "  ssh rc=$? (informational -- the connection can drop as the host goes down)"
  sleep 45
  ssh root@192.168.5.1 "qm status $1" | grep -q stopped || ssh root@192.168.5.1 "qm stop $1 --timeout 120"
  ssh root@192.168.5.1 "qm start $1"
  sleep 60
  ssh root@192.168.5.1 "qm agent $1 ping" >/dev/null 2>&1 && echo "  agent OK" || echo "  AGENT STILL NOT RESPONDING"
}

node_ready() {   # $1 = node name
  kubectl --context local-k3s get node "$1" \
    -o jsonpath='{.metadata.name}{" ready="}{.status.conditions[?(@.type=="Ready")].status}{"\n"}'
}
```

**`shutdown -h now` is deliberately not suppressed.** It previously ended in
`2>/dev/null || true`, which hid a `sudo` auth failure and then hard-stopped the VM
45 seconds later anyway — the node goes down, the reason it went down hard is
invisible, and the run looks identical to a clean shutdown. That is the
self-certifying-failure pattern this plan refuses everywhere else. The `ssh` exit
status is *not* the signal (the connection can legitimately drop mid-shutdown);
**stderr is**. `Sorry, try again` or `incorrect password` means the guest was never
asked to shut down and the `qm stop` below is a hard stop on a running node. On a
node that has already ignored ACPI shutdown once (102 and 103 both have) a hard stop
is not automatically wrong — but it must be a fact you know, not one you assume.

**Do not paste 4a-4d as one block.** A literal executor running them back to back
power-cycles the entire cluster with 60-second gaps.

- [ ] **Step 4a: Cycle VM 101 (`k3s-node-2`)**

```sh
cycle_vm 101 k3s-node-2
node_ready k3s-node-2
```

Expected: `agent OK`, then `k3s-node-2 ready=True`. If either is wrong, **stop
here** — do not cycle another VM. See this task's rollback trigger.

- [ ] **Step 4b: Cycle VM 102 (`k3s-node-3`) — only after 4a reported `ready=True`**

```sh
cycle_vm 102 k3s-node-3
node_ready k3s-node-3
```

Expected: `agent OK`, then `k3s-node-3 ready=True`. Stop on anything else.

- [ ] **Step 4c: Cycle VM 103 (`k3s-node-1`) — only after 4b reported `ready=True`**

```sh
cycle_vm 103 k3s-node-1
node_ready k3s-node-1
```

Expected: `agent OK`, then `k3s-node-1 ready=True`. Stop on anything else.

- [ ] **Step 4d: Cycle VM 100 (`k3s-controller`) last — only after 4c reported `ready=True`**

This is the API server, and it is a single control-plane node with no HA. The API
is unavailable for the whole of this step, which is also why it goes last: a worker
that failed to come back in 4a-4c is far easier to diagnose while the API still
answers.

```sh
cycle_vm 100 k3s-controller
for i in $(seq 1 30); do kubectl --context local-k3s get --raw /readyz >/dev/null 2>&1 && { echo "API ready"; break; }; sleep 10; done
kubectl --context local-k3s get --raw /readyz >/dev/null || echo "ABORT: API not ready 300s after the controller restart"
kubectl --context local-k3s get nodes
```

Expected: `agent OK`, `API ready` with no `ABORT`, and all 4 nodes `Ready`
(controller `Ready,SchedulingDisabled`).

- [ ] **Step 5: Verify the agent responds (post-change state)**

```sh
for id in 100 101 102 103; do printf "VM %s: " $id; ssh root@192.168.5.1 "qm agent $id ping" >/dev/null 2>&1 && echo "AGENT OK" || echo "FAILED"; done
kubectl --context local-k3s get nodes
```

Expected: `AGENT OK` ×4, and all 4 nodes `Ready` (controller `Ready,SchedulingDisabled`).

- [ ] **Step 6: Confirm the snapshot guard now passes**

```sh
./proxmox/snapshot-cluster.sh create agent-verify && ./proxmox/snapshot-cluster.sh delete agent-verify
```

Expected: `OK: snapshot set 'agent-verify' created` then `OK: ... deleted`.

**Rollback trigger:** a node fails to return `Ready` within 5 minutes of start. Recovery: `ssh root@192.168.5.1 "qm start <id>"`, then check `journalctl -u k3s-agent` on that node. No snapshot exists yet, so do not attempt a rollback — diagnose forward.

---

## Task 2: Prove the rollback actually works

An untested rollback is an assumption, not a safety net. Everything after this task depends on it.

**Files:** none — the "Cluster snapshots" section of `proxmox/README.md` was
committed in Phase A. This task verifies it.

**Interfaces:**
- Consumes: working guest agent (Task 1).
- Produces: verified rollback procedure.

- [ ] **Step 1: Create the test snapshot**

```sh
./proxmox/snapshot-cluster.sh create rollbacktest
./proxmox/snapshot-cluster.sh list
```

Expected: `rollbacktest` present on all 4 VMs and on `main-pool/k3s-nfs`.

- [ ] **Step 2: Make a change on both storage layers that rollback must undo**

The canaries are the entire proof. If either one is never written — a `sudo -S`
auth failure with stderr suppressed, or `main-pool/k3s-nfs` not actually mounted
at `/main-pool/k3s-nfs` — then Step 4 finds it absent afterwards and reports
success for a rollback that proved nothing. **A write that fails must be
indistinguishable from nothing, not from success**, so read each canary back and
hard-fail if it does not come back.

Run this as one block. The parentheses make it a subshell so the `exit 1` ends
the step rather than your login shell, and so a failure on the first canary stops
the second from being written. Each result is captured into a variable and then
tested — **not** piped into `grep -q`, which exits on match and can `SIGPIPE` the
`ssh` upstream, failing the pipeline under `pipefail` *because* it matched (the
same trap `snapshot-cluster.sh` documents around its pool-health check).

```sh
(
set -uo pipefail
PW=$(tr -d '\n' < ~/sudo-pw.txt)

vm=$(printf '%s\n' "$PW" | ssh -o BatchMode=yes grant@k3s-node-1 \
  'sudo -S -p "" sh -c "echo ROLLBACK_CANARY > /root/rollback-canary.txt; cat /root/rollback-canary.txt"')
[ "$vm" = "ROLLBACK_CANARY" ] || {
  echo "ABORT: VM canary did not read back from k3s-node-1:/root/rollback-canary.txt"
  echo "       got: '${vm}'  -- check for a sudo auth failure or a full/ro disk."
  echo "       Do NOT run Step 3: a rollback whose canary was never written"
  echo "       cannot prove anything, and Step 4 would report success."
  exit 1
}

nfs=$(ssh root@192.168.5.1 \
  'echo ROLLBACK_CANARY > /main-pool/k3s-nfs/rollback-canary.txt; cat /main-pool/k3s-nfs/rollback-canary.txt')
[ "$nfs" = "ROLLBACK_CANARY" ] || {
  echo "ABORT: NFS canary did not read back from /main-pool/k3s-nfs/rollback-canary.txt"
  echo "       got: '${nfs}'  -- confirm the dataset is mounted there:"
  echo "       ssh root@192.168.5.1 'zfs get -H -o value mountpoint,mounted main-pool/k3s-nfs'"
  echo "       Do NOT run Step 3."
  exit 1
}

# Receipt, written on the workstation -- the one place the rollback does not
# touch. Step 4 refuses to accept "canary gone" as proof unless this exists.
date -u +%FT%TZ > /tmp/rollback-canary-written.txt
echo "OK: both canaries written and read back; receipt in /tmp/rollback-canary-written.txt"
)
```

Expected: `OK: both canaries written and read back`. Neither file existed when
the Step 1 snapshot was taken, so the rollback must remove both.

- [ ] **Step 3: Roll back**

```sh
./proxmox/snapshot-cluster.sh rollback rollbacktest
```

Type `rollbacktest` at the confirmation prompt. Takes several minutes (stops, rolls back, restarts 4 VMs).

- [ ] **Step 4: Verify both canaries are gone and the cluster is healthy**

"Absent afterwards" only means something if they were present beforehand, so
assert Step 2's receipt first. Without it, an absent canary is equally consistent
with a working rollback and with a canary that was never written.

```sh
test -f /tmp/rollback-canary-written.txt || {
  echo "ABORT: no Step 2 receipt. The canaries were never confirmed written, so"
  echo "       'canary gone' below would prove nothing. Re-run Step 1-3 properly;"
  echo "       do not record this rollback as verified."
}
cat /tmp/rollback-canary-written.txt   # when the canaries were confirmed written
```

Expected: the receipt exists and its timestamp is **before** the Step 3 rollback.
If the `ABORT` prints, stop here — the rest of this step cannot be interpreted.

```sh
PW=$(tr -d '\n' < ~/sudo-pw.txt)
printf '%s\n' "$PW" | ssh -o BatchMode=yes grant@k3s-node-1 \
  'sudo -S -p "" sh -c "test -f /root/rollback-canary.txt && echo CANARY STILL PRESENT || echo canary gone"' 2>/dev/null
ssh root@192.168.5.1 'test -f /main-pool/k3s-nfs/rollback-canary.txt && echo "NFS CANARY STILL PRESENT" || echo "nfs canary gone"'
```

Expected: `canary gone` and `nfs canary gone`. Note that `2>/dev/null` on the
first line means a sudo failure here also prints nothing at all — an *empty* line
is not `canary gone` and must be treated as a failed check, not a pass.

Four VMs have just been restarted, so confirm the API is actually answering
before reading anything from it:

```sh
for i in $(seq 1 30); do kubectl --context local-k3s get --raw /readyz >/dev/null 2>&1 && { echo "API ready"; break; }; sleep 10; done
kubectl --context local-k3s get --raw /readyz >/dev/null || echo "ABORT: API not ready 300s after rollback -- that is itself a rollback failure"
kubectl --context local-k3s get nodes
if pods=$(kubectl --context local-k3s get pods -A --field-selector=status.phase!=Succeeded); then
  printf '%s\n' "$pods" | awk 'NR>1 && $4 != "Running" && $4 != "Completed" { n++; print } END { if (!n) print "no unhealthy pods" }'
else
  echo "ABORT: cannot list pods; the cluster did not come back from the rollback"
fi
```

The `if` is what makes this trustworthy: `no unhealthy pods` is printed **only**
on the branch where the listing actually succeeded, so it can never stand in for
"nothing answered".

Expected: `API ready`, 4 nodes `Ready`, and no unexpected non-Running pods beyond the known `svclb-lb-unifi` `Pending` ones.

**If either canary survives, the rollback mechanism is broken — stop the entire plan and diagnose. Do not proceed to any upgrade.** The same applies if the receipt is missing: an unproven rollback and a broken one are the same thing as far as every later task is concerned.

- [ ] **Step 5: Compare against the Task 0 baseline**

```sh
BASE=.superpowers/sdd/2026-09-13-k3s-cluster-upgrade-phases-0-4/baseline
test -s "$BASE/lb.txt" || echo "ABORT: no baseline -- regenerate it per Task 0 Step 3 before reading this diff"
kubectl --context local-k3s get svc -A -o json | jq -r '.items[]|select(.spec.type=="LoadBalancer")|"\(.metadata.namespace)/\(.metadata.name) \((.status.loadBalancer.ingress//[])|map(.ip)|join(","))"' | sort > "$BASE/after-rollbacktest-lb.txt"
diff "$BASE/lb.txt" "$BASE/after-rollbacktest-lb.txt" && echo "LB IPs unchanged"
```

Expected: `LB IPs unchanged`, with no `ABORT`. If the baseline is missing, Task 0
Step 3 says how to regenerate it and what the result must match — a `diff` against
a file that does not exist proves nothing.

- [ ] **Step 6: Delete the test snapshot and the receipt**

The receipt is removed too. Leaving it behind would let a *second* run of this
task pass Step 4's assertion on a receipt from the first run — the exact
self-certification Step 2 exists to prevent.

```sh
./proxmox/snapshot-cluster.sh delete rollbacktest
rm -f /tmp/rollback-canary-written.txt
```

- [ ] **Step 7: Verify the committed documentation**

The **Cluster snapshots** section of `proxmox/README.md` was written in Phase A
(`0526b27`) and extended by `f0dc943` with the "Only the most recent set can be
rolled back to" subsection. **Do not rewrite it.** Confirm it is present and
unmodified:

```sh
git status --porcelain -- proxmox/README.md
git log --oneline -1 -- proxmox/README.md
grep -n "^## Cluster snapshots" proxmox/README.md
grep -n "^### Only the most recent set" proxmox/README.md
```

Expected: no `git status` output, most recent commit `f0dc943` or a later
reviewed fix, and both headings found.

Then read that section and check it still matches what you just observed —
`create`'s pre-flight, the label rules, the one-gate-at-a-time constraint, and
`rollback`'s all-or-nothing behaviour.
It deliberately makes **no claim that rollback has been verified end-to-end**,
because until this task runs, it has not been. If this task's rollback test
passed, that is worth recording; open a follow-up rather than editing the file
mid-task, and do not commit anything here.

**Rollback trigger:** n/a — this task *is* the rollback test.

---

## Task 3: Extend the controller root filesystem

`/` is a 31G LV at 69% (9.2G free). `ubuntu-vg` has **31.00g free**, so no VM disk resize is needed.

**Files:** none in repo

- [ ] **Step 1: Record the pre-change state**

```sh
PW=$(tr -d '\n' < ~/sudo-pw.txt)
printf '%s\n' "$PW" | ssh -o BatchMode=yes grant@k3s-controller 'sudo -S -p "" sh -c "vgs ubuntu-vg; df -h /"' 2>/dev/null
```

Expected: `VFree` ≈ `31.00g`, `/` size `31G`, ~9.2G available.

- [ ] **Step 2: Gate — take a snapshot**

`lvextend` is the only step in this plan that cannot be undone in place: LVM has
no "shrink the LV back" that is safe against a filesystem that has already grown
onto the new extents. The snapshot set is therefore the *only* route back, which
makes this gate mandatory rather than precautionary. It was missing.

```sh
./proxmox/snapshot-cluster.sh create pre-lvextend
```

Expected: `OK: snapshot set 'pre-lvextend' created`. **If it aborts, do not run
Step 3.** Without the gate there is no way back from Step 3 at all.

- [ ] **Step 3: Extend the LV and grow the filesystem**

`ext4` supports online resize, so no downtime.

```sh
PW=$(tr -d '\n' < ~/sudo-pw.txt)
printf '%s\n' "$PW" | ssh -o BatchMode=yes grant@k3s-controller \
  'sudo -S -p "" sh -c "lvextend -l +100%FREE -r /dev/ubuntu-vg/ubuntu-lv"' 2>/dev/null
```

Expected: `Size of logical volume ubuntu-vg/ubuntu-lv changed from 31.00 GiB ... to <61.x> GiB` and `The filesystem on /dev/mapper/ubuntu--vg-ubuntu--lv is now ... blocks long.`

- [ ] **Step 4: Verify the post-change state**

```sh
PW=$(tr -d '\n' < ~/sudo-pw.txt)
printf '%s\n' "$PW" | ssh -o BatchMode=yes grant@k3s-controller 'sudo -S -p "" df -h /' 2>/dev/null
kubectl --context local-k3s get nodes k3s-controller
```

Expected: `/` ≈ 61G with ~39G available and usage ~34%; node still `Ready,SchedulingDisabled`.

- [ ] **Step 5: Delete the gate snapshot**

Only after Step 4 has passed. While this snapshot exists it is the one thing that
can undo Step 3, so do not release it on the strength of `lvextend`'s own output —
release it once `df` and the node's `Ready` status both confirm the result.

```sh
./proxmox/snapshot-cluster.sh delete pre-lvextend
```

Expected: `OK: snapshot set 'pre-lvextend' deleted`.

**Rollback trigger:** `lvextend` errors, or `/` becomes read-only, or the node does not return to `Ready,SchedulingDisabled`. Recovery: `./proxmox/snapshot-cluster.sh rollback pre-lvextend`. LV extension is not reversible in place, so this gate is the only route back — there is no forward fix to attempt first.

---

## Task 4: Widen the Velero schedule

Velero currently backs up 6 of ~31 namespaces, omitting `monitoring`, `truelist-staging` (MariaDB + Redis), `infra`, `unifi`, `wprb-rocks`, `pmbot` and `cert-manager`. This is a second safety net behind snapshots, not a replacement.

**Files:** none — `velero/values.yml` was updated and committed in Phase A. This
task verifies it, then applies it to the cluster.

> **STOP — two open questions must be answered by a human before Step 5.** Step 5
> is the first backup taken under the widened scope, and it is not reversible in
> the way the rest of this task is: once cluster-wide Secrets are in the bucket,
> they are in the bucket. Both questions are written out in full under **"Open
> questions — for the human, not the executing agent"** in
> `docs/superpowers/specs/2026-09-12-k3s-cluster-upgrade-design.md`. In brief:
>
> 1. **Key management and bucket access for the Secrets now in scope.** The
>    bucket has SSE-S3 (AES256) applied at bucket level, so the objects are
>    encrypted — the open part is whether SSE-S3 is sufficient for cert-manager's
>    ACME key, every TLS private key, the registry and DB credentials and the
>    Tailscale OAuth secret, or whether SSE-KMS with a customer-managed key and an
>    audited bucket policy is warranted.
> 2. **The first widened backup will probably exceed this task's own 60-minute
>    rollback trigger** (harbor ~107Gi, tv-channel 75Gi, monitoring's 50Gi
>    Prometheus TSDB, elk 20G), and filesystem-backing a live TSDB or Elasticsearch
>    data directory is unlikely to restore cleanly. Prefer excluding those
>    *volumes* via `backup.velero.io/backup-volumes-excludes` over excluding the
>    namespaces.
>
> **If you are executing task-by-task and have not seen answers to both, stop at
> the end of Step 4 and ask.** Steps 1-4 are safe to run: they change the Schedule
> object, which `helm rollback` restores, and take no backup.

- [ ] **Step 1: Record the pre-change state**

```sh
kubectl --context local-k3s get schedules.velero.io velero-homelab-daily -n velero -o jsonpath='{.spec.template.includedNamespaces}'; echo
helm --kube-context local-k3s history velero -n velero
kubectl --context local-k3s get backups.velero.io -n velero --sort-by=.metadata.creationTimestamp \
  -o custom-columns='NAME:.metadata.name,PHASE:.status.phase,ITEMS:.status.progress.totalItems' | tail -3
```

Expected: `["home-assistant","dev-box","openclaw","openclaw-dottie","openclaw-stonk","immich"]`

**Write down two numbers from this step.** Nothing else records either, and both
are needed later:

- **The current `deployed` revision** from `helm history`. Step 3 expects the next
  one, and the rollback trigger needs this one as its target. Do not assume `2` —
  the release history is whatever the cluster says it is.
- **`ITEMS` from the most recent `Completed` backup.** Step 5 compares against it.
  The spec recorded `447` when it was written; that figure is now days old and
  moves with the cluster, so read the live value rather than trusting either the
  spec or this plan.

- [ ] **Step 2: Verify the committed values file**

`velero/values.yml` was switched from `includedNamespaces` to
`excludedNamespaces` in Phase A (`460ab05`). **Do not edit it.** Confirm it is
present and unmodified, and read the resulting deny-list:

```sh
git status --porcelain -- velero/values.yml
git log --oneline -1 -- velero/values.yml
grep -n "cludedNamespaces" velero/values.yml
```

Expected: no `git status` output, most recent commit `460ab05`, and
`excludedNamespaces` present with no `includedNamespaces`.

Everything not in that deny-list is now included. `longhorn-system` and `signoz`
are excluded because they have been `Terminating` for 2y+ and would only generate
errors. Note the exact list before Step 3 — Step 5 reuses it verbatim in a
one-off Backup, and the two must match or the verification proves nothing.

- [ ] **Step 3: Apply**

The `vmware-tanzu` alias is a property of the local Helm config, not of the
cluster, so it may not exist on this workstation at all — and if it does, its
cache may predate chart 12.1.0. Either way `helm upgrade` fails on the chart
reference rather than on anything meaningful. Tasks 8 and 10 both do this; Task 4
did not.

**`--set-file credentials.secretContents.cloud` is mandatory on every `velero`
upgrade, not just the install.** The chart renders the `velero` Secret from
`.Values.credentials.secretContents` on every render. That key is **not** in
`velero/values.yml` — it holds the AWS access key, so it lives in the gitignored
`velero/credentials-velero` and is passed with `--set-file` (see
`velero/README.md` step 4). Upgrading without it re-renders the Secret with an
empty `data:`, which blanks Velero's S3 credentials: the next backup — Step 5 of
this very task — fails to reach the bucket. This is the same shape as the
Tailscale `operator-oauth` trap in Task 8, arrived at by a different route.

Confirm the file is there before doing anything else; if it is missing, recover
it from the IAM access key rather than upgrading without it:

```sh
test -s velero/credentials-velero && echo "credentials present" || \
  echo "ABORT: velero/credentials-velero missing -- the upgrade would blank Velero's S3 access"
```

```sh
helm repo add vmware-tanzu https://vmware-tanzu.github.io/helm-charts 2>/dev/null; helm repo update vmware-tanzu >/dev/null
helm search repo vmware-tanzu/velero --version 12.1.0
helm upgrade velero vmware-tanzu/velero --kube-context local-k3s -n velero \
  --version 12.1.0 -f velero/values.yml \
  --set-file credentials.secretContents.cloud=./velero/credentials-velero
```

Expected: `credentials present`, `helm search repo` lists chart `12.1.0` (if it
lists nothing, the repo cache is still stale or the version does not exist —
stop, do not run the upgrade), then `STATUS: deployed` with `REVISION` exactly
one higher than the revision recorded in Step 1.

Then prove the Secret still carries the key, before Step 5 depends on it:

```sh
kubectl --context local-k3s get secret velero -n velero \
  -o jsonpath='{.data.cloud}' | base64 -d | grep -c aws_access_key_id
```

Expected: `1`. A `0`, an empty output or a `NotFound` means the credential was
blanked — **stop and roll back** rather than letting Step 5 fail on it.

- [ ] **Step 4: Verify the schedule changed**

```sh
kubectl --context local-k3s get schedules.velero.io velero-homelab-daily -n velero -o jsonpath='{.spec.template}'; echo
```

Expected: `excludedNamespaces` present, `includedNamespaces` absent.

- [ ] **Step 5: Run a real backup and confirm it completes**

```sh
kubectl --context local-k3s create -f - <<'EOF'
apiVersion: velero.io/v1
kind: Backup
metadata:
  generateName: widen-verify-
  namespace: velero
  labels:
    velero.io/schedule-name: velero-homelab-daily
spec:
  excludedNamespaces: [kube-system, kube-public, kube-node-lease, velero, longhorn-system, signoz]
  ttl: 168h
EOF
```

Then poll until terminal:

```sh
kubectl --context local-k3s get backups.velero.io -n velero --sort-by=.metadata.creationTimestamp \
  -o custom-columns='NAME:.metadata.name,PHASE:.status.phase,ERR:.status.errors,ITEMS:.status.progress.totalItems' | tail -3
```

Expected: `PHASE=Completed`, `ERR=<none>`, and `ITEMS` substantially higher than the pre-change `ITEMS` you recorded in Step 1 (now covering ~25 namespaces rather than 6). The spec's reading was **447** (`docs/superpowers/specs/2026-09-12-k3s-cluster-upgrade-design.md`); compare against the live Step 1 figure, not against either number written down here.

If `PartiallyFailed`, list the failures before deciding anything:
```sh
kubectl --context local-k3s get podvolumebackups.velero.io -n velero -o json | \
  jq -r '.items[]|select(.status.phase!="Completed")|"\(.status.phase) \(.spec.pod.namespace)/\(.spec.pod.name) vol=\(.spec.volume) \(.status.message//"")"'
```

- [ ] **Step 6: Nothing to commit**

`velero/values.yml` is already committed (`460ab05`). Confirm the tree is clean
for it instead of attempting a commit that would have nothing staged:

```sh
git status --porcelain -- velero/values.yml
```

Expected: no output.

**Rollback trigger:** backup phase is `Failed` (not `PartiallyFailed`), or runtime exceeds 60 minutes. Recovery: `helm rollback velero <prev-rev> --kube-context local-k3s -n velero`, where `<prev-rev>` is the `deployed` revision recorded in Step 1 — re-read it with `helm --kube-context local-k3s history velero -n velero` rather than assuming a number. There is no snapshot gate on this task and none is needed: the change is confined to the Helm release and the Schedule object it owns, both of which `helm rollback` restores.

---

## Task 5: Resolve the two failed Helm releases

Both have a `deployed` earlier revision serving traffic; only the latest upgrade attempt failed. Clearing them removes ambiguity before any upgrade work.

**Files:** none in repo

- [ ] **Step 1: Record the pre-change state**

```sh
helm --kube-context local-k3s list -A --failed
```

Expected: `home-assistant` (rev 7) and `openclaw-stonk` (rev 4), both `failed`.

- [ ] **Step 2: Roll `home-assistant` back to its last good revision**

Revision 7 failed with `updates to statefulset spec for fields other than 'replicas', 'ordinals', 'template', 'updateStrategy', 'persistentVolumeClaimRetentionPolicy' and 'minReadySeconds' are forbidden` — the chart tried to change an immutable StatefulSet field. Revision 6 is the same chart version (`0.3.54`), so rolling back is close to a no-op.

```sh
helm rollback home-assistant 6 --kube-context local-k3s -n home-assistant --wait --timeout 5m
helm --kube-context local-k3s status home-assistant -n home-assistant | head -4
kubectl --context local-k3s get pods -n home-assistant
```

Expected: `STATUS: deployed`, and `home-assistant-0` `Running`.

- [ ] **Step 3: Roll `openclaw-stonk` back to its last good revision**

Revision 4 failed with `conflict with "kubectl-patch" using apps/v1: .spec.template.spec.volumes[name="data"].persistentVolumeClaim.claimName` — someone `kubectl patch`ed the claim name, so Helm's apply conflicts over field ownership. Revision 3 is the same chart version.

```sh
helm rollback openclaw-stonk 3 --kube-context local-k3s -n openclaw-stonk --wait --timeout 5m
helm --kube-context local-k3s status openclaw-stonk -n openclaw-stonk | head -4
kubectl --context local-k3s get pods -n openclaw-stonk
```

Expected: `STATUS: deployed`, pod `Running`.

If the rollback fails with the same conflict, the live `claimName` differs from the chart's. Read the live value and decide explicitly which is correct — do **not** blindly force:
```sh
kubectl --context local-k3s get deploy openclaw-stonk -n openclaw-stonk \
  -o jsonpath='{.spec.template.spec.volumes[?(@.name=="data")].persistentVolumeClaim.claimName}'; echo
```

- [ ] **Step 4: Verify no failed releases remain**

```sh
helm --kube-context local-k3s list -A --failed
```

Expected: no rows.

**Rollback trigger:** a workload that was `Running` before stops being `Running`, or the `openclaw-stonk` data PVC changes identity.

**Recovery — and there is no snapshot gate here, deliberately.** Nothing in this
task takes one, and no earlier gate is still alive by the time it runs (Task 3
deletes `pre-lvextend` at its own last step, and Task 2 deletes `rollbacktest` at
Step 6), so "roll back the cluster snapshot" is not an option that exists. Do not
go looking for one mid-incident.

What this task actually changes is two Helm releases, and Helm's own history is
the way back:

```sh
helm --kube-context local-k3s history home-assistant -n home-assistant
helm --kube-context local-k3s rollback home-assistant 7 -n home-assistant   # back to the failed rev
```

Rolling forward to the revision you came from is reversible, so try that first.
If a pod still cannot start after that, the cause is a pre-existing problem with
the workload — these releases were *already* `failed` before this task touched
them, and rev 6/3 are the same chart versions as 7/4. Diagnose it as a workload
fault. If you decide you need the option of a whole-cluster rollback before
digging further, **take a snapshot first** (`./proxmox/snapshot-cluster.sh create
pre-helm-triage`) rather than acting as though one already exists — and delete it
when you are done.

---

## Task 6: Disable k3s ServiceLB

`k3s server` runs with no arguments, so klipper ServiceLB is enabled alongside MetalLB. It creates hostPort DaemonSets for all 5 LoadBalancer services; `svclb-lb-unifi` has been `Pending` for **650 days** because it needs 6 hostPorts (UniFi 80/443/8080/8443/8843/8880) while `svclb-traefik` already holds 80/443. `svclb-traefik` also shows 92-104 restarts.

MetalLB performs the actual IP assignment, so klipper is redundant.

**Files:** **modify the top-level `README.md` (Step 9)** — Step 4 creates
`/etc/rancher/k3s/config.yaml` on the controller, which makes two statements in
that file false, and Step 9 corrects and commits them. `k3s/config.yaml` and
`k3s/README.md` were authored and committed in Phase A; this task verifies those
two and installs `config.yaml` on the controller without editing either.

This task and Task 7 are the only two in the plan that write a repo file.

**Interfaces:**
- Produces: `--disable=servicelb` in effect; 5 `svclb-*` DaemonSets removed; hostPorts 80/443 freed on all nodes.

- [ ] **Step 1: Record the pre-change state**

```sh
BASE=.superpowers/sdd/2026-09-13-k3s-cluster-upgrade-phases-0-4/baseline
kubectl --context local-k3s get ds -n kube-system | grep svclb
kubectl --context local-k3s get pods -A | grep -c svclb
test -s "$BASE/lb.txt" || echo "ABORT: no baseline -- regenerate it per Task 0 Step 3 before taking the gate"
cp "$BASE/lb.txt" "$BASE/pre-servicelb-lb.txt"; cat "$BASE/pre-servicelb-lb.txt"
```

Expected: 5 `svclb-*` DaemonSets, 15 pods (3 of them `Pending`), and the 5 MetalLB IPs.

- [ ] **Step 2: Gate — take a snapshot**

```sh
./proxmox/snapshot-cluster.sh create pre-servicelb
```

Expected: `OK: snapshot set 'pre-servicelb' created`.

- [ ] **Step 3: Verify the committed k3s config file**

`k3s/config.yaml` was authored and committed in Phase A (`c69c00c`). **Do not
rewrite it.** Confirm it is present and unmodified, then read it:

```sh
git status --porcelain -- k3s/config.yaml
git log --oneline -1 -- k3s/config.yaml
cat k3s/config.yaml
```

Expected: no `git status` output, most recent commit `c69c00c`, and the file
setting `disable: [servicelb]` and the `node-role.kubernetes.io/control-plane:NoExecute`
node-taint. Step 4 echoes this same content back from the controller — know what
it should look like before you compare.

- [ ] **Step 4: Install it on the controller and restart k3s**

stderr is deliberately **not** suppressed anywhere here. Each of these four must
succeed, and `sudo -S` reports an auth failure only on stderr — hiding it would
let the config silently not install while the next step carried on regardless.
The `tee` line feeds the password and the file down a single stdin stream: a pipe
and a `< file` both target ssh's stdin, and under bash/sh the redirect wins, so
`sudo -S` would read `# Declarative k3s server config...` as the password. (zsh's
`MULTIOS` concatenates them and happens to work, which is what made this hard to
spot.)

```sh
PW=$(tr -d '\n' < ~/sudo-pw.txt)
printf '%s\n' "$PW" | ssh -o BatchMode=yes grant@k3s-controller 'sudo -S -p "" mkdir -p /etc/rancher/k3s'
{ printf '%s\n' "$PW"; cat k3s/config.yaml; } | \
  ssh -o BatchMode=yes grant@k3s-controller 'sudo -S -p "" tee /etc/rancher/k3s/config.yaml >/dev/null'
printf '%s\n' "$PW" | ssh -o BatchMode=yes grant@k3s-controller 'sudo -S -p "" cat /etc/rancher/k3s/config.yaml'
printf '%s\n' "$PW" | ssh -o BatchMode=yes grant@k3s-controller 'sudo -S -p "" systemctl restart k3s'
```

Expected: no `sudo`/`ssh` errors on stderr, the file echoes back correctly (all 14
lines, starting `# Declarative k3s server config`), then a brief API outage
(~30-60s) while k3s restarts. A `Sorry, try again` or `incorrect password` means
the config was **not** written — stop, do not proceed to Step 5.

- [ ] **Step 5: Wait for the API and verify svclb is gone**

k3s has just been restarted, which is exactly when the API is *not* answering. A
bare `grep svclb || echo "no svclb DaemonSets"` prints the success string when the
`kubectl` fails, so "the DaemonSets are gone" and "the API never replied" are the
same output — and this step is the whole point of the task. Wait for the API,
hard-fail if it does not come back, then separate the listing from the matching so
a failed listing cannot be read as an empty result.

```sh
for i in $(seq 1 20); do kubectl --context local-k3s get --raw /readyz >/dev/null 2>&1 && { echo "API ready"; break; }; sleep 10; done
kubectl --context local-k3s get --raw /readyz >/dev/null || {
  echo "ABORT: API not ready 200s after the k3s restart. This is the rollback"
  echo "       trigger for this task -- do not interpret the checks below, they"
  echo "       cannot tell 'svclb is gone' from 'nothing answered'."
}

if ds=$(kubectl --context local-k3s get ds -n kube-system -o name); then
  printf '%s\n' "$ds" | awk '/svclb/ { n++; print } END { if (!n) print "no svclb DaemonSets" }'
else
  echo "ABORT: cannot list DaemonSets -- API not answering. This is NOT 'svclb is gone'."
fi

if pods=$(kubectl --context local-k3s get pods -A -o name); then
  printf '%s\n' "$pods" | awk '/svclb/ { n++; print } END { if (!n) print "no svclb pods" }'
else
  echo "ABORT: cannot list pods -- API not answering. This is NOT 'svclb is gone'."
fi
```

Expected: `API ready`, then `no svclb DaemonSets` and `no svclb pods`, with no `ABORT` anywhere. The `if` is the point: each success string is printed only on the branch where the listing succeeded, so it cannot be produced by a failed `kubectl`.

- [ ] **Step 6: Verify all 5 LoadBalancer IPs survived — this is the acceptance test**

```sh
BASE=.superpowers/sdd/2026-09-13-k3s-cluster-upgrade-phases-0-4/baseline
sleep 30
kubectl --context local-k3s get svc -A -o json | jq -r '.items[]|select(.spec.type=="LoadBalancer")|"\(.metadata.namespace)/\(.metadata.name) \((.status.loadBalancer.ingress//[])|map(.ip)|join(","))"' | sort > "$BASE/post-servicelb-lb.txt"
diff "$BASE/pre-servicelb-lb.txt" "$BASE/post-servicelb-lb.txt" && echo "ALL 5 LB IPs UNCHANGED"
curl -s -o /dev/null -w "traefik 192.168.20.1 -> HTTP %{http_code}\n" --max-time 10 http://192.168.20.1/ || echo "traefik unreachable"
```

Expected: `ALL 5 LB IPs UNCHANGED`, and traefik returns an HTTP status (404 is fine — it proves the listener is up).

- [ ] **Step 7: Verify the two alerts this was causing have cleared**

```sh
sleep 120
kubectl --context local-k3s exec -n monitoring prometheus-kube-prometheus-stack-prometheus-0 -c prometheus -- \
  wget -qO- 'http://localhost:9090/api/v1/alerts' 2>/dev/null | \
  jq -r '[.data.alerts[]|select(.labels.alertname|test("KubePodNotReady|KubeDaemonSetRolloutStuck"))]|length'
```

Expected: `0` (was 5 — 3 `KubePodNotReady` + 2 `KubeDaemonSetRolloutStuck`).

- [ ] **Step 8: Verify the committed `k3s/README.md`**

`k3s/README.md` was authored in Phase A across `c69c00c`, `57cd3ca`, `146fd27`
and `2c2fbcc` (the last two fix real traps: agents need `K3S_URL`/`K3S_TOKEN` on
every install, and the config install must carry the sudo password and the file
down one stdin stream). **Do not rewrite it.** Confirm it is present and
unmodified:

```sh
git status --porcelain -- k3s/README.md
git log --oneline -1 -- k3s/README.md
grep -n "^## " k3s/README.md
```

Expected: no `git status` output, most recent commit `2c2fbcc`, and at least the
`## config.yaml` and `## Upgrading` sections.

Read `## Upgrading` now — Task 12 executes exactly that procedure, and the
`K3S_URL`/`K3S_TOKEN` warning there is the one that matters most.

- [ ] **Step 9: Correct the top-level `README.md`**

Step 4 creates `/etc/rancher/k3s/config.yaml` on the controller, which makes two
statements in the top-level `README.md` false. Nothing else in this plan corrects
them, and `k3s/README.md` does not: it documents the file without touching the
older claims that the file does not exist.

```sh
grep -n "config.yaml" README.md
```

The two to fix:

1. **`README.md:30`** — "There is no `/etc/rancher/k3s/config.yaml` on any node --
   all configuration is default." Replace with the truth after Step 4, which is
   narrower than a blanket statement in either direction — the *controller* has
   one; the three workers still do not:

   ```markdown
   The controller has `/etc/rancher/k3s/config.yaml`; the three worker nodes have
   none and run entirely on defaults. The repo copy is `k3s/config.yaml`, and
   `k3s/README.md` documents what it sets and how to reinstall it.
   ```

2. **The note under *Taints* (~`README.md:47`)** — "**Important:** ... To make it
   persistent, create `/etc/rancher/k3s/config.yaml` on the controller:" followed
   by a `node-taint` snippet. This now instructs the reader to create a file that
   already exists, and to add a setting that is already in it. Rewrite it as a
   statement of fact:

   ```markdown
   **Important:** This taint is on the node object, not applied by the command
   above on every boot. It is persisted in `/etc/rancher/k3s/config.yaml` on the
   controller (see `k3s/config.yaml` in this repo), so a k3s reinstall that keeps
   that file re-applies it. A reinstall that *replaces* the file does not.
   ```

Keep the `node-taint` YAML if it reads as an illustration of what the file
contains; drop it if it reads as an instruction. Then commit — this is the only
repo file Task 6 changes, so stage it alone:

```sh
git diff -- README.md
git add README.md
git commit -m "record the controller's config.yaml in the top-level README

Task 6 installed /etc/rancher/k3s/config.yaml on the controller, which makes the
README's 'there is no config.yaml on any node' false and turns the 'to make it
persistent, create ...' note into an instruction to create a file that already
exists. The workers still have none, so say controller rather than any node." -- README.md
```

Expected: `README.md` no longer claims the file is absent and no longer tells the
reader to create it; `git status --porcelain -- README.md` is clean afterwards.

- [ ] **Step 10: Delete the gate snapshot**

`k3s/config.yaml` and `k3s/README.md` are already committed, so there is nothing
to stage for those two. Confirm the tree is clean rather than attempting a commit:

```sh
git status --porcelain -- k3s/config.yaml k3s/README.md README.md
./proxmox/snapshot-cluster.sh delete pre-servicelb
```

Expected: no `git status` output (Step 9's commit has already landed `README.md`),
then `OK: snapshot set 'pre-servicelb' deleted`.

**Rollback trigger:** any of the 5 LoadBalancer IPs missing or changed, traefik unreachable, or the API not ready within 200s. Recovery: `./proxmox/snapshot-cluster.sh rollback pre-servicelb`.

---

## Task 7: Diagnose the 171-day-failed certificate

`truelist-staging/truelist-stag-io-cert` has been `READY=False` for 171 days with a live `cm-acme-http-solver-sqcfr` pod.

**This task survived the removal of the cert-manager upgrade, and is still worth
running.** Its original justification was "diagnose before upgrading cert-manager so
a pre-existing failure is not mistaken for upgrade fallout"; with no cert-manager
upgrade in scope, that reason is gone. Two independent ones remain: the failure is
real and undocumented, and **Task 12 restarts the API server and every node**, after
which an undocumented 171-day-old `READY=False` certificate is indistinguishable from
k3s-upgrade fallout in Task 12's own acceptance test (Step 6 reads
`get certificates -A`). Recording it first is what keeps that check readable.

**Files:** Modify `cert-manager/README.md` (Step 4). This and Task 6 Step 9
(top-level `README.md`) are the only two repo writes in the plan; every other task
verifies already-committed artifacts. The *certificate* fix, if any, is a follow-up
— this task only records the finding.

- [ ] **Step 1: Capture the failure reason**

```sh
kubectl --context local-k3s describe certificate truelist-stag-io-cert -n truelist-staging | tail -25
kubectl --context local-k3s get certificaterequests,orders,challenges -n truelist-staging 2>&1 | head -20
```

- [ ] **Step 2: Read the challenge state — this usually names the cause**

```sh
kubectl --context local-k3s get challenges -n truelist-staging -o json 2>/dev/null | \
  jq -r '.items[]|"state=\(.status.state) reason=\(.status.reason//"-") dns=\(.spec.dnsName) type=\(.spec.solver.http01//.spec.solver.dns01|keys|join(","))"'
```

Expected: a `pending` challenge with a reason such as an HTTP-01 self-check failure — consistent with `truelist-api-stag.grant.dev` not resolving to `192.168.20.1` from outside, or Let's Encrypt being unable to reach it.

- [ ] **Step 3: Check whether the name resolves publicly**

```sh
getent hosts truelist-api-stag.grant.dev || echo "does not resolve locally"
curl -s -o /dev/null -w "HTTP %{http_code}\n" --max-time 10 \
  "http://truelist-api-stag.grant.dev/.well-known/acme-challenge/test" || echo "unreachable"
```

- [ ] **Step 4: Record the finding**

Append this to `cert-manager/README.md`, filling the bracketed values from Steps 1-3:

```markdown
## Known issues

### truelist-stag-io-cert has been failing since ~2026-03-25

`truelist-staging/truelist-stag-io-cert` is `READY=False` and has been for 171
days as of 2026-09-13. A `cm-acme-http-solver-sqcfr` pod and an orphan solver
Ingress for `truelist-api-stag.grant.dev` are still present.

- Challenge state: `[state from Step 2]`
- Reason: `[reason from Step 2]`
- Resolves publicly: `[yes/no from Step 3]`

Recorded before the k3s upgrade in Task 12 so this pre-existing failure is not
misread as upgrade fallout. cert-manager itself is **not** being upgraded — it
stays on v1.14.5; see the spec's "cert-manager: removed from scope".

**Not yet fixed.** If the hostname is not meant to be reachable from the
internet, HTTP-01 cannot work and the fix is either a DNS-01 solver or deleting
the Certificate and its solver Ingress. That is a decision, not a cleanup.
```

**Do not attempt a fix in this task.**

- [ ] **Step 5: Commit**

```sh
git add cert-manager/README.md
git commit -m "record long-failing truelist staging certificate

truelist-stag-io-cert has been READY=False for 171 days with a stuck ACME
solver. Task 12 restarts the API server and every node, and its acceptance test
reads 'get certificates -A'; recording this first is what keeps that check
readable. cert-manager itself is not being upgraded." -- cert-manager/README.md
```

**Rollback trigger:** none — read-only investigation.

---

## Task 8: Upgrade the Tailscale operator 1.76.6 → 1.102.3

26 releases. The operator manages ~25 annotation-driven egress proxies used for cross-cluster Prometheus scraping. **kubectl access does not depend on it** — that runs through the node's own `tailscaled`.

**Files:** none — `tailscale/values.yml` and `tailscale/README.md` were authored
and committed in Phase A. This task verifies them, then applies the upgrade.

**Interfaces:**
- Consumes: verified rollback (Task 2).
- Produces: operator at 1.102.3, OAuth secret no longer in Helm values.

- [ ] **Step 1: Record the pre-change state**

```sh
helm --kube-context local-k3s list -n tailscale
helm --kube-context local-k3s history tailscale-operator -n tailscale
kubectl --context local-k3s get pods -n tailscale --no-headers | wc -l
kubectl --context local-k3s exec -n monitoring prometheus-kube-prometheus-stack-prometheus-0 -c prometheus -- \
  wget -qO- 'http://localhost:9090/api/v1/targets?state=active' 2>/dev/null | \
  jq -r '[.data.activeTargets[]|select(.labels.cluster=="ovh" or .labels.cluster=="production")]|group_by(.health)|.[]|"\(.[0].health): \(length)"'
```

Record the operator version, the pod count, and how many OVH/production targets are `up`. That target count is the acceptance criterion in Step 8.

**Also record the current `deployed` revision** from `helm history`. The rollback
trigger needs it, and nothing else in this task captures it — do not assume `1`
just because the release has only ever been upgraded once.

- [ ] **Step 2: Rotate the OAuth credential and move it out of Helm values**

The current values hold `oauth.clientSecret` in plaintext, so they cannot be committed. The chart creates the `operator-oauth` Secret from those values, and that Secret **already exists** (671d old) — but it is owned by the Helm release, not adopted from outside it. Step 3 deals with the consequence; do not skip it.

**Rotate the credential first** — it was exposed in a terminal session on 2026-09-13. Create a new OAuth client in the Tailscale admin console with the same scopes, then:

```sh
kubectl --context local-k3s create secret generic operator-oauth -n tailscale \
  --from-literal=client_id='<new-client-id>' \
  --from-literal=client_secret='<new-client-secret>' \
  --dry-run=client -o yaml | kubectl --context local-k3s apply -f -
```

- [ ] **Step 3: Protect the Secret from Helm's pruner**

`operator-oauth` is owned by the Helm release (`app.kubernetes.io/managed-by: Helm`,
`meta.helm.sh/release-name: tailscale-operator`), and the chart only renders it
when `oauth.clientId` is set. Upgrading with `tailscale/values.yml`, which
deliberately omits it, drops the Secret from the rendered manifest — and Helm
deletes resources that are in the old manifest and absent from the new one. That
would destroy the credential you just rotated in Step 2 and break every egress
proxy carrying cross-cluster Prometheus scraping. Annotate it first:

```sh
kubectl --context local-k3s annotate secret operator-oauth -n tailscale \
  helm.sh/resource-policy=keep --overwrite
kubectl --context local-k3s get secret operator-oauth -n tailscale \
  -o jsonpath='{.metadata.annotations.helm\.sh/resource-policy}'; echo
```

Expected: `keep`. **If this does not print `keep`, stop — do not run the upgrade.**

This must happen after Step 2 (the Secret must exist to be annotated) and before
the upgrade in Step 7. `tailscale/README.md` documents the same requirement under
"Protect the Secret before upgrading".

- [ ] **Step 4: Verify the committed secret-free values**

`tailscale/values.yml` was authored and committed in Phase A (`5ca448b`). **Do
not rewrite it.** Confirm it is present, unmodified, and free of credentials:

```sh
git status --porcelain -- tailscale/values.yml
git log --oneline -1 -- tailscale/values.yml
cat tailscale/values.yml
grep -iE "client_secret|clientSecret|tskey-" tailscale/values.yml && echo "SECRET IN VALUES - STOP" || echo "no secret in values"
```

Expected: no `git status` output, most recent commit `5ca448b`, `no secret in
values`, and the only setting being `operatorConfig.logging`.

- [ ] **Step 5: Gate — snapshot**

```sh
./proxmox/snapshot-cluster.sh create pre-tailscale
```

- [ ] **Step 6: Diff the rendered output before applying**

A 26-release jump can move the values schema, so inspect rather than assume.

```sh
helm repo add tailscale https://pkgs.tailscale.com/helmcharts 2>/dev/null; helm repo update tailscale >/dev/null
helm template tailscale-operator tailscale/tailscale-operator --kube-context local-k3s \
  -n tailscale --version 1.102.3 -f tailscale/values.yml > /tmp/ts-new.yaml
grep -c "^kind:" /tmp/ts-new.yaml
grep -E "^kind:" /tmp/ts-new.yaml | sort | uniq -c
grep -iE "client_secret|tskey-" /tmp/ts-new.yaml && echo "SECRET IN RENDER - STOP" || echo "no secret in rendered output"
```

Expected: a plausible set of kinds (Deployment, ServiceAccount, RBAC, CRDs) and `no secret in rendered output`. **If a secret appears, stop** — the chart is still templating it and Step 2 needs revisiting.

The absence of a `Secret` kind here is exactly why Step 3 is mandatory: the
resource is gone from the new manifest but present in the old one, which is the
condition Helm prunes on. Confirm Step 3 was done before continuing:

```sh
kubectl --context local-k3s get secret operator-oauth -n tailscale \
  -o jsonpath='{.metadata.annotations.helm\.sh/resource-policy}'; echo
```

Expected: `keep`.

- [ ] **Step 7: Upgrade**

```sh
helm upgrade tailscale-operator tailscale/tailscale-operator --kube-context local-k3s \
  -n tailscale --version 1.102.3 -f tailscale/values.yml --wait --timeout 10m
kubectl --context local-k3s rollout status deploy/operator -n tailscale --timeout=5m
```

Expected: `STATUS: deployed`, `REVISION` one higher than the revision recorded in Step 1, rollout complete.

- [ ] **Step 8: Verify the Secret survived and the proxies came back — acceptance test**

Check the Secret first. If it is gone, the proxies cannot recover no matter how
long you wait, and the target counts below are meaningless.

```sh
kubectl --context local-k3s get secret operator-oauth -n tailscale \
  -o jsonpath='{.metadata.name}' 2>/dev/null || echo "SECRET GONE - operator will fail"
echo
```

Expected: `operator-oauth`. If it prints `SECRET GONE - operator will fail`,
Helm pruned it — stop and recover the credential (re-create it from the Tailscale
admin console as in Step 2, then re-apply Step 3) before judging anything else.

The `ts-*` StatefulSets are recreated, so targets will flap briefly. Allow time before judging.

`grep -vc Running || echo "all running"` cannot be trusted here: `grep -c` exits
non-zero when it matches nothing, *and* when the `kubectl` upstream fails, so both
"every pod is Running" and "the API did not answer" print `all running`. Capture
the listing, fail on the listing, then judge the contents.

```sh
sleep 180
if pods=$(kubectl --context local-k3s get pods -n tailscale --no-headers); then
  printf '%s\n' "$pods" | awk 'NF && $3 != "Running" { n++; print } END { if (!n) print "all running" }'
else
  echo "ABORT: cannot list tailscale pods -- this is a failure, not 'all running'"
fi
kubectl --context local-k3s exec -n monitoring prometheus-kube-prometheus-stack-prometheus-0 -c prometheus -- \
  wget -qO- 'http://localhost:9090/api/v1/targets?state=active' 2>/dev/null | \
  jq -r '[.data.activeTargets[]|select(.labels.cluster=="ovh" or .labels.cluster=="production")]|group_by(.health)|.[]|"\(.[0].health): \(length)"'
```

Expected: every `tailscale` pod `Running`, and the `up` count matching Step 1. If lower after 5 minutes, check `kubectl logs -n tailscale deploy/operator`.

- [ ] **Step 9: Verify the committed `tailscale/README.md`**

`tailscale/README.md` was authored and committed in Phase A (`b8141a3`). **Do not
rewrite it.** Confirm it is present and unmodified:

```sh
git status --porcelain -- tailscale/README.md
git log --oneline -1 -- tailscale/README.md
grep -n "^#\{2,3\} " tailscale/README.md
```

Expected: no `git status` output, most recent commit `b8141a3`, and sections
including "The OAuth credential is not in this repo", "Protect the Secret before
upgrading", "What it does here" and "Upgrading".

The install snippet in that file uses a `<version>` placeholder on purpose — the
repo's prose must never assert a live version, because it goes stale silently.
Do not substitute `1.102.3` into it.

- [ ] **Step 10: Delete the gate snapshot**

`tailscale/values.yml` and `tailscale/README.md` are already committed, so there
is nothing to stage. Confirm the tree is clean rather than attempting a commit:

```sh
git status --porcelain -- tailscale/values.yml tailscale/README.md
./proxmox/snapshot-cluster.sh delete pre-tailscale
```

Expected: no `git status` output, then `OK: snapshot set 'pre-tailscale' deleted`.

**Rollback trigger:** the `operator-oauth` Secret is missing after the upgrade, OVH/production `up` target count does not return to the Step 1 value within 5 minutes, or the operator pod crash-loops. Recovery: `helm rollback tailscale-operator <prev-rev> --kube-context local-k3s -n tailscale`, where `<prev-rev>` is the `deployed` revision recorded in Step 1 (`helm --kube-context local-k3s history tailscale-operator -n tailscale` if you did not write it down); if that fails, `./proxmox/snapshot-cluster.sh rollback pre-tailscale`. Note that `helm rollback` will **not** bring back a pruned Secret with a new credential in it — that has to be re-created from the Tailscale admin console.

---

## Task 9: Upgrade MetalLB v0.14.5 → v0.16.1

Installed from raw manifests, not Helm. Config is minimal: one `IPAddressPool` (`192.168.20.1-255`) and one `L2Advertisement`, no BGP. **This is load-bearing** — it provides the IP for Traefik, which fronts all 6 Ingresses, plus UniFi and the registry.

**Files:** none — `metallb/README.md` was updated and committed in Phase A. This
task verifies it.

- [ ] **Step 1: Record the pre-change state**

```sh
BASE=.superpowers/sdd/2026-09-13-k3s-cluster-upgrade-phases-0-4/baseline
kubectl --context local-k3s get deploy,ds -n metallb-system -o jsonpath='{range .items[*]}{.metadata.name}{"\t"}{.spec.template.spec.containers[*].image}{"\n"}{end}'
kubectl --context local-k3s get ipaddresspools,l2advertisements -A
test -s "$BASE/lb.txt" || echo "ABORT: no baseline -- regenerate it per Task 0 Step 3 before taking the gate"
cat "$BASE/lb.txt"
```

Expected: images at `v0.14.5`, pool `192.168.20.1-255`, the 5 LB IPs, and no `ABORT`.

- [ ] **Step 2: Back up the CRs (they must survive the manifest apply)**

This lands in the SDD workspace rather than `/tmp` for the same reason as the Task 0
baseline: it is the input to this task's recovery path, and recovery is exactly when
a `tmpfs` `/tmp` will have been through a reboot.

```sh
BASE=.superpowers/sdd/2026-09-13-k3s-cluster-upgrade-phases-0-4/baseline
mkdir -p "$BASE/metallb-backup"
kubectl --context local-k3s get ipaddresspools,l2advertisements -n metallb-system -o yaml > "$BASE/metallb-backup/crs.yaml"
grep -c "kind:" "$BASE/metallb-backup/crs.yaml"
git status --porcelain -- .superpowers/   # must print nothing
```

Expected: at least 2, and no `git status` output.

- [ ] **Step 3: Gate — snapshot**

```sh
./proxmox/snapshot-cluster.sh create pre-metallb
```

- [ ] **Step 4: Apply the new manifest**

```sh
kubectl --context local-k3s apply -f https://raw.githubusercontent.com/metallb/metallb/v0.16.1/config/manifests/metallb-native.yaml
```

Expected: a mix of `configured` and `created`, no errors.

- [ ] **Step 5: Wait for rollout**

```sh
kubectl --context local-k3s rollout status deploy/controller -n metallb-system --timeout=5m
kubectl --context local-k3s rollout status ds/speaker -n metallb-system --timeout=5m
kubectl --context local-k3s get pods -n metallb-system
```

Expected: controller `1/1`, all 3 speakers `Running`.

- [ ] **Step 6: Verify version and that the CRs survived**

```sh
kubectl --context local-k3s get deploy,ds -n metallb-system -o jsonpath='{range .items[*]}{.metadata.name}{"\t"}{.spec.template.spec.containers[*].image}{"\n"}{end}'
kubectl --context local-k3s get ipaddresspools,l2advertisements -A
```

Expected: images at `v0.16.1`, pool and advertisement unchanged.

- [ ] **Step 7: Verify LB IPs and real traffic — acceptance test**

```sh
BASE=.superpowers/sdd/2026-09-13-k3s-cluster-upgrade-phases-0-4/baseline
sleep 30
kubectl --context local-k3s get svc -A -o json | jq -r '.items[]|select(.spec.type=="LoadBalancer")|"\(.metadata.namespace)/\(.metadata.name) \((.status.loadBalancer.ingress//[])|map(.ip)|join(","))"' | sort > "$BASE/post-metallb-lb.txt"
diff "$BASE/lb.txt" "$BASE/post-metallb-lb.txt" && echo "ALL 5 LB IPs UNCHANGED"
for ip in 192.168.20.1 192.168.20.10 192.168.20.50; do
  printf "%s -> " $ip
  curl -s -o /dev/null -w "HTTP %{http_code}\n" --max-time 10 "http://$ip/" || echo "unreachable"
done
kubectl --context local-k3s get ingress -A
```

Expected: `ALL 5 LB IPs UNCHANGED`, each IP answering (any HTTP status proves L2 works), and all 6 Ingresses still showing `192.168.20.1`.

- [ ] **Step 8: Verify the committed README and delete the gate snapshot**

`metallb/README.md` was already updated in Phase A (`cd193d1`): the manifest URL
was bumped `v0.14.5` → `v0.16.1` and the stale `metallb-config.yaml` reference
corrected to `metallb-config.yml`. **Nothing to edit and nothing to commit.**

```sh
git status --porcelain -- metallb/README.md
git log --oneline -1 -- metallb/README.md
grep -n "v0\.1[46]\.\|metallb-config" metallb/README.md
```

Expected: no `git status` output, most recent commit `cd193d1`, the manifest URL
at `v0.16.1` with no `v0.14.5` remaining, and `metallb-config.yml`. The URL must
match the one you applied in Step 4.

```sh
./proxmox/snapshot-cluster.sh delete pre-metallb
```

**Rollback trigger:** any LB IP missing/changed, or any of the 3 probed IPs unreachable after 2 minutes.

**Recovery:**

```sh
BASE=.superpowers/sdd/2026-09-13-k3s-cluster-upgrade-phases-0-4/baseline
kubectl --context local-k3s apply -f https://raw.githubusercontent.com/metallb/metallb/v0.14.5/config/manifests/metallb-native.yaml
kubectl --context local-k3s apply --server-side --force-conflicts -f "$BASE/metallb-backup/crs.yaml"
```

`--server-side --force-conflicts` is not optional on the second command.
`crs.yaml` is `kubectl get -o yaml` output, so every object in
it carries `metadata.resourceVersion`, `uid` and `creationTimestamp` from when it
was read. A client-side `apply` of that is rejected — or worse, applied with a
stale `resourceVersion` that conflicts with the live object — and it also collides
with whichever field manager owns those fields now. Server-side apply ignores the
read-only metadata and `--force-conflicts` takes ownership rather than erroring
mid-recovery. (Stripping the metadata first is equally valid if you prefer:
`kubectl ... -o yaml | yq 'del(.items[].metadata.resourceVersion, .items[].metadata.uid, .items[].metadata.creationTimestamp)'`.)

If the IPs still do not return, `./proxmox/snapshot-cluster.sh rollback pre-metallb`.

---

## Tasks 10 and 11: cert-manager — REMOVED FROM SCOPE

**There is no Task 10 and no Task 11.** Both were cert-manager upgrades
(v1.14.5 → v1.17.4, then v1.17.4 → v1.21.2). They have been deleted from this
plan. cert-manager stays on the installed **v1.14.5** for the whole of Phases 0-4.

**This is not a blocked step waiting for a precondition.** cert-manager 1.14
supports Kubernetes **1.24 → 1.31**, so it gates nothing here: Task 12's patch bump
inside 1.29 does not touch it, and it would survive the first two minor hops of a
future Phase 5+ unaided. Nothing in Tasks 0-9 or Task 12 depends on it moving.

**Why it was removed rather than corrected.** The cert-manager reasoning in this
project was confidently wrong three separate times, and each time the wrong version
was written down as settled. Rather than attempt a fourth correction inside a plan
that executes against a live production cluster, the work has been taken out of
scope and the *verified* facts — including which of the previous claims were false
and why — recorded in the spec instead.

**Before anyone puts cert-manager back into a plan, read
`docs/superpowers/specs/2026-09-12-k3s-cluster-upgrade-design.md`, section
"cert-manager: removed from scope".** In particular, the live CRDs' protection is
*not* what the deleted tasks claimed, and getting that wrong cascades to every
Certificate, CertificateRequest, Issuer, ClusterIssuer, Order and Challenge in the
cluster. Derive the next attempt from the chart source, not from these tasks and
not from release notes.

**Task numbering is unchanged on purpose.** Task 12 is still Task 12; the gap here
is deliberate and is explained at the top of this file. Skip from Task 9 to Task 12.

**No gate is involved.** The `pre-certmanager-117` and `pre-certmanager-121`
snapshot labels belonged to these tasks and are never created. If either label
exists on the Proxmox host, something ran a superseded version of this plan —
investigate before deleting it, because deleting a snapshot set discards the route
back past that point.

---

## Task 12: Upgrade k3s v1.29.4 → v1.29.15+k3s1 (patch only)

No API changes within a patch release, so this validates the upgrade *mechanism* — binary swap, systemd restart, drain/uncordon order — at minimal risk before any minor hop.

**Files:** none. No step in this task edits a repo file. `k3s/README.md` already
documents this procedure and was finalised in Phase A (`2c2fbcc`) — this task
*executes* what it describes; it does not change it. The version numbers here are
plan-local, and the README deliberately uses `vX.Y.Z+k3sN` placeholders rather
than asserting what is running.

- [ ] **Step 1: Record the pre-change state**

```sh
kubectl --context local-k3s get nodes -o wide
kubectl --context local-k3s get pods -A --field-selector=status.phase!=Succeeded | grep -vc Running || true
```

Expected: all nodes `v1.29.4+k3s1`.

- [ ] **Step 2: Gate — snapshot**

```sh
./proxmox/snapshot-cluster.sh create pre-k3s-1-29-15
```

- [ ] **Step 3: Upgrade the controller**

```sh
PW=$(tr -d '\n' < ~/sudo-pw.txt)
printf '%s\n' "$PW" | ssh -o BatchMode=yes grant@k3s-controller \
  'sudo -S -p "" sh -c "curl -sfL https://get.k3s.io | INSTALL_K3S_VERSION=v1.29.15+k3s1 sh -"' 2>/dev/null | tail -5
```

Expected: installer output ending in `systemd: Starting k3s`.

- [ ] **Step 4: Wait for the API and confirm the server version**

```sh
for i in $(seq 1 24); do kubectl --context local-k3s get --raw /readyz >/dev/null 2>&1 && { echo "API ready"; break; }; sleep 10; done
kubectl --context local-k3s get nodes
```

Expected: `k3s-controller` at `v1.29.15+k3s1`, workers still `v1.29.4+k3s1` (expected — agents lag until Step 5).

- [ ] **Step 5: Upgrade agents one at a time, draining each**

Workloads on `local-path` PVCs cannot reschedule elsewhere — they stay down until their node returns. This is expected, not a fault.

**A failed drain stops the loop; it is not tolerated.** This previously read
`kubectl drain … || true`, which swallowed the failure and went straight on to
replace the k3s binary and restart the agent on a node that still had workloads
scheduled on it. The two realistic causes both matter: a PodDisruptionBudget that
cannot be satisfied, or an eviction that times out at 300s. Neither is a reason to
proceed — the drain exists so the restart lands on an empty node.

`--force` is deliberately **not** added. It would make the drain succeed by deleting
pods with no controller behind them, which on this cluster is a decision about
specific workloads, not a flag to sprinkle on an upgrade loop. If you conclude the
drain cannot succeed without it, that is a finding to act on knowingly for the node
in front of you — not a change to make to all three.

Note this interacts with a known property of the cluster rather than a fault:
**23 PVCs use `local-path` with `WaitForFirstConsumer`**, so their pods cannot
reschedule and the drain evicts them into `Pending` until the node returns. That is
expected and the drain still succeeds; a drain that *fails* is something else.

`K3S_URL` and `K3S_TOKEN` are **required** on every agent. The installer chooses
server vs agent from the environment alone — it has no awareness of the existing
`k3s-agent.service` — and rewrites `k3s-agent.service.env` from scratch rather
than reading it. Omitting them installs a second k3s in **server** mode on the
worker and never restarts the agent, so the node is not upgraded.

```sh
PW=$(tr -d '\n' < ~/sudo-pw.txt)
TOKEN=$(printf '%s\n' "$PW" | ssh -o BatchMode=yes grant@k3s-controller \
  'sudo -S -p "" cat /var/lib/rancher/k3s/server/node-token' 2>/dev/null | tr -d '\n')
echo "node-token: ${#TOKEN} chars"     # length only -- never print the token
[ -n "$TOKEN" ] || echo "ABORT: no node-token; do not continue"
for h in k3s-node-1 k3s-node-2 k3s-node-3; do
  [ -n "$TOKEN" ] || break
  echo "===== $h"
  if ! kubectl --context local-k3s drain $h --ignore-daemonsets --delete-emptydir-data --timeout=300s; then
    echo "ABORT: drain of $h failed. NOT swapping the k3s binary on an undrained node."
    echo "       Read the drain output above, decide what is holding it, then either"
    echo "       resolve it or re-run this loop for the remaining nodes by hand."
    break
  fi
  printf '%s\n' "$PW" | ssh -o BatchMode=yes grant@$h \
    "sudo -S -p '' sh -c 'curl -sfL https://get.k3s.io | INSTALL_K3S_VERSION=v1.29.15+k3s1 K3S_URL=https://k3s-controller:6443 K3S_TOKEN=$TOKEN sh -'" 2>/dev/null | tail -3
  sleep 45
  kubectl --context local-k3s uncordon $h
  for i in $(seq 1 18); do
    V=$(kubectl --context local-k3s get node $h -o jsonpath='{.status.nodeInfo.kubeletVersion}' 2>/dev/null)
    R=$(kubectl --context local-k3s get node $h -o jsonpath='{.status.conditions[?(@.type=="Ready")].status}' 2>/dev/null)
    echo "  $h version=$V ready=$R"
    [ "$V" = "v1.29.15+k3s1" ] && [ "$R" = "True" ] && break
    sleep 10
  done
done
```

Expected: `node-token: <N> chars` with N greater than 0 before anything is drained — if it is `0`, the token read failed and the loop must not run. Then each node reaches `v1.29.15+k3s1` and `ready=True` before the loop moves on. **If a node does not, stop — do not drain the next one.** Each installer run should report `systemd: Starting k3s-agent`; `systemd: Starting k3s` would mean a server was installed on a worker — stop and roll back.

- [ ] **Step 6: Full verification against the Task 0 baseline — acceptance test**

```sh
BASE=.superpowers/sdd/2026-09-13-k3s-cluster-upgrade-phases-0-4/baseline
test -s "$BASE/lb.txt" || echo "ABORT: no baseline -- regenerate it per Task 0 Step 3 before reading this diff"
kubectl --context local-k3s get nodes -o wide
kubectl --context local-k3s get svc -A -o json | jq -r '.items[]|select(.spec.type=="LoadBalancer")|"\(.metadata.namespace)/\(.metadata.name) \((.status.loadBalancer.ingress//[])|map(.ip)|join(","))"' | sort > "$BASE/post-k3s-lb.txt"
diff "$BASE/lb.txt" "$BASE/post-k3s-lb.txt" && echo "ALL 5 LB IPs UNCHANGED"
kubectl --context local-k3s get ingress -A
kubectl --context local-k3s get certificates -A
curl -s -o /dev/null -w "traefik -> HTTP %{http_code}\n" --max-time 10 http://192.168.20.1/
```

The pod check needs separating from its own success message. Four nodes have just
had their k3s binary replaced, so `| grep -vE "Running|Completed" || echo "no
unhealthy pods"` prints the reassuring string both when every pod is healthy and
when the API server never answered — in the acceptance test for a k3s upgrade,
those two must not look alike:

```sh
kubectl --context local-k3s get --raw /readyz >/dev/null || echo "ABORT: API not answering -- that is itself an acceptance-test failure"
if pods=$(kubectl --context local-k3s get pods -A --field-selector=status.phase!=Succeeded); then
  printf '%s\n' "$pods" | awk 'NR>1 && $4 != "Running" && $4 != "Completed" { n++; print } END { if (!n) print "no unhealthy pods" }'
else
  echo "ABORT: cannot list pods -- do NOT read this as 'no unhealthy pods'"
fi
```

Expected: all 4 nodes `v1.29.15+k3s1` and `Ready`; `ALL 5 LB IPs UNCHANGED`; 6 Ingresses on `192.168.20.1`; the same 4 `True` certificates; `no unhealthy pods` with no `ABORT` above it; traefik answering.

- [ ] **Step 7: Confirm the Proxmox host monitoring still reports**

```sh
kubectl --context local-k3s exec -n monitoring prometheus-kube-prometheus-stack-prometheus-0 -c prometheus -- \
  wget -qO- 'http://localhost:9090/api/v1/query?query=zpool_health' 2>/dev/null | \
  jq -r '.data.result[]|"\(.metric.pool) => \(.value[1])"'
```

Expected: `bulk-pool => 0` and `main-pool => 0`.

- [ ] **Step 8: Confirm the tree is clean and delete the gate snapshot**

This task changed no repo files, so there is nothing to commit. Attempting one
would stage nothing and `git commit` would exit non-zero, failing the task at its
last step. Verify instead:

```sh
git status --porcelain -- k3s/
```

Expected: no output. If `k3s/README.md` *is* dirty, something edited it during
the upgrade — review the diff and decide deliberately, rather than committing it
as part of this task.

Then release the gate:

```sh
./proxmox/snapshot-cluster.sh delete pre-k3s-1-29-15
```

Expected: `OK: snapshot set 'pre-k3s-1-29-15' deleted`.

Record the outcome in the plan ledger (check off this task's boxes) — that is the
completion record for Task 12, not a commit.

**Rollback trigger:** any node fails to reach `v1.29.15+k3s1` and `Ready` within 3 minutes of uncordon; any LB IP changes; traefik unreachable; any previously-`True` certificate goes `False`. Recovery: `./proxmox/snapshot-cluster.sh rollback pre-k3s-1-29-15` — downgrading k3s in place is not supported, so the snapshot is the only route back.

---

## Completion

At the end of Task 12:

- Rollback is proven, not assumed
- Tailscale operator and MetalLB are current
- cert-manager is **untouched, still on v1.14.5, and still EOL** (upstream EOL Oct
  2024). That is a known, accepted gap, not an outcome of this plan — the upgrade was
  removed from scope, not attempted and abandoned. It is **not** blocking anything
  here: 1.14 supports Kubernetes 1.24 → 1.31. See the spec, "cert-manager: removed
  from scope".
- k3s is patch-current on 1.29 and the upgrade mechanism is validated
- klipper/MetalLB conflict resolved, clearing 5 of the 6 firing warning groups
- Velero covers ~25 namespaces instead of 6 — **namespaced objects only.** `includeClusterResources` is left unset, and Velero's auto-rule only defaults it to true when the backup is unrestricted; a non-empty `excludedNamespaces` keeps it false. So cluster-scoped objects — CRDs, ClusterRoles/Bindings, StorageClasses, ClusterIssuers, PVs — are still **not** backed up, and the snapshot set remains the only thing covering them. Setting `includeClusterResources: true` is a separate decision with its own restore implications.
- Two secrets (Slack webhook, Tailscale OAuth) are out of Helm values
- k3s configuration is declarative and survives reinstalls

**Explicitly NOT done:** Kubernetes minor hops 1.30 → 1.36, and **any cert-manager
upgrade at all** — cert-manager is out of scope for this plan and remains on the EOL
v1.14.5. Before planning the hops, re-sample `apiserver_requested_deprecated_apis`
after ≥7 days of API server uptime — the earlier reading was taken after only 2.5h and
proves nothing. The open question of in-place hops versus a rebuild at 1.36 is
recorded in the spec, as is what a future cert-manager attempt must verify first.
