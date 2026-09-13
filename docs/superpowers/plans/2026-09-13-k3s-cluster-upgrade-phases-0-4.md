# k3s Cluster Upgrade — Phases 0-4 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the homelab cluster safe to upgrade (tested rollback + fix pre-existing breakage), then bring the Tailscale operator, MetalLB, and cert-manager current and validate the k3s upgrade mechanism with a patch-level bump.

**Architecture:** Every change is gated behind a Proxmox VM snapshot plus a ZFS snapshot of the NFS dataset, which together capture the entire cluster state atomically. Work proceeds lowest-risk first so the rollback mechanism is proven before anything important depends on it. Kubernetes minor version hops (1.30 → 1.36) are explicitly **out of scope** and deferred to a later plan.

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
| cert-manager | v1.14.5 | v1.21.2 (via v1.17.2) |

---

## File Structure

> **All of these files are already written and committed** (Phase A). This plan's
> remaining work is the live cluster operations. Where a task previously said
> "create/modify X", it now says "verify the committed X" and points at the
> commit. **Do not re-author any of them from this document** — the repo is the
> source of truth, and a copy here would drift. The one exception is Task 7
> Step 4, which appends a "Known issues" section to `cert-manager/README.md`
> using values that can only be read from the live cluster.

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
| `cert-manager/install-crd.sh` | Pinned version, `crds.enabled`/`crds.keep` instead of `installCRDs` |
| `cert-manager/README.md` | What `install-crd.sh` installs, webhook admission probe, known-failing cert |

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

- [ ] **Step 2: Record the baseline everything is compared against**

```sh
kubectl --context local-k3s get nodes -o wide > /tmp/baseline-nodes.txt
kubectl --context local-k3s get pods -A --field-selector=status.phase!=Succeeded > /tmp/baseline-pods.txt
kubectl --context local-k3s get svc -A -o json | jq -r '.items[]|select(.spec.type=="LoadBalancer")|"\(.metadata.namespace)/\(.metadata.name) \((.status.loadBalancer.ingress//[])|map(.ip)|join(","))"' > /tmp/baseline-lb.txt
kubectl --context local-k3s get ingress -A > /tmp/baseline-ingress.txt
kubectl --context local-k3s get certificates -A > /tmp/baseline-certs.txt
wc -l /tmp/baseline-*.txt
```

Expected: 5 non-empty files. `baseline-lb.txt` must contain exactly these 5 lines:
```
kube-system/traefik 192.168.20.1
unifi/lb-unifi 192.168.20.10
unifi/lb-unifi-udp 192.168.20.4
infra/registry 192.168.20.50
wprb-rocks/wprb-rocks-backend-service 192.168.20.2
```

- [ ] **Step 3: Verify the committed snapshot script**

`proxmox/snapshot-cluster.sh` was authored and reviewed in Phase A — `16bfb36`
(initial), `f8afe22` (whole-set pre-flight), `3a11cfe` (verify VMs stopped before
rollback), `7016922` (pool-health guard no longer races a pipeline). **Do not
rewrite it.** Read it, then confirm it is present, executable and unmodified:

```sh
test -x proxmox/snapshot-cluster.sh && bash -n proxmox/snapshot-cluster.sh && echo "script OK"
git status --porcelain -- proxmox/snapshot-cluster.sh
git log --oneline -1 -- proxmox/snapshot-cluster.sh
```

Expected: `script OK`, no output from `git status` (clean), and the most recent
commit touching it is `7016922` or a later Phase A/B fix — not a local edit.

Read the file before using it. The behaviour every later task depends on:
`create` aborts unless all pools are healthy, `qemu-guest-agent` answers on all
4 VMs, and the label is unused on all 5 targets; `rollback` aborts unless the
label is present on all 5 targets and every VM has actually stopped; `delete` is
idempotent but fails loudly if a target refuses to give the snapshot up.

- [ ] **Step 4: Confirm it refuses to run (agent not installed yet)**

```sh
./proxmox/snapshot-cluster.sh create preflight-check
```

Expected: `ABORT: qemu-guest-agent not responding on VM 100`.
This is the correct pre-change state — the guard works, and Task 1 installs the agent.

- [ ] **Step 5: Nothing to commit**

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

- [ ] **Step 4: Power-cycle VMs one at a time to add the device**

A reboot from inside the guest is **not** enough — the virtio-serial device is attached at VM start. Do the workers first and the controller last so the API stays up as long as possible.

**The VMID→hostname mapping is not sequential.** Verified via MAC address against the ARP table and each node's `INTERNAL-IP`:

| VMID | Proxmox name | Hostname | IP |
|---|---|---|---|
| 100 | k3s-controller | `k3s-controller` | 192.168.10.1 |
| 101 | k3s-worker-2 | `k3s-node-2` | 192.168.10.3 |
| 102 | k3s-worker-3 | `k3s-node-3` | 192.168.10.4 |
| 103 | k3s-worker-1 | `k3s-node-1` | 192.168.10.2 |

```sh
PW=$(tr -d '\n' < ~/sudo-pw.txt)
cycle_vm() {   # $1 = vmid, $2 = hostname
  echo "== cycling VM $1 ($2)"
  printf '%s\n' "$PW" | ssh -o BatchMode=yes grant@"$2" 'sudo -S -p "" shutdown -h now' 2>/dev/null || true
  sleep 45
  ssh root@192.168.5.1 "qm status $1" | grep -q stopped || ssh root@192.168.5.1 "qm stop $1 --timeout 120"
  ssh root@192.168.5.1 "qm start $1"
  sleep 60
  ssh root@192.168.5.1 "qm agent $1 ping" >/dev/null 2>&1 && echo "  agent OK" || echo "  AGENT STILL NOT RESPONDING"
}

cycle_vm 101 k3s-node-2
cycle_vm 102 k3s-node-3
cycle_vm 103 k3s-node-1
cycle_vm 100 k3s-controller
```

Expected: `agent OK` after each. Run them one at a time and confirm the node returns `Ready` before cycling the next:

```sh
kubectl --context local-k3s get nodes
```

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

```sh
PW=$(tr -d '\n' < ~/sudo-pw.txt)
printf '%s\n' "$PW" | ssh -o BatchMode=yes grant@k3s-node-1 \
  'sudo -S -p "" sh -c "echo ROLLBACK_CANARY > /root/rollback-canary.txt; cat /root/rollback-canary.txt"'
ssh root@192.168.5.1 'echo ROLLBACK_CANARY > /main-pool/k3s-nfs/rollback-canary.txt; cat /main-pool/k3s-nfs/rollback-canary.txt'
```

Expected: `ROLLBACK_CANARY` printed twice. These files did **not** exist when the snapshot was taken.

- [ ] **Step 3: Roll back**

```sh
./proxmox/snapshot-cluster.sh rollback rollbacktest
```

Type `rollbacktest` at the confirmation prompt. Takes several minutes (stops, rolls back, restarts 4 VMs).

- [ ] **Step 4: Verify both canaries are gone and the cluster is healthy**

```sh
PW=$(tr -d '\n' < ~/sudo-pw.txt)
printf '%s\n' "$PW" | ssh -o BatchMode=yes grant@k3s-node-1 \
  'sudo -S -p "" sh -c "test -f /root/rollback-canary.txt && echo CANARY STILL PRESENT || echo canary gone"' 2>/dev/null
ssh root@192.168.5.1 'test -f /main-pool/k3s-nfs/rollback-canary.txt && echo "NFS CANARY STILL PRESENT" || echo "nfs canary gone"'
kubectl --context local-k3s get nodes
kubectl --context local-k3s get pods -A --field-selector=status.phase!=Succeeded | grep -vE "Running|Completed" | head
```

Expected: `canary gone`, `nfs canary gone`, 4 nodes `Ready`, and no unexpected non-Running pods beyond the known `svclb-lb-unifi` `Pending` ones.

**If either canary survives, the rollback mechanism is broken — stop the entire plan and diagnose. Do not proceed to any upgrade.**

- [ ] **Step 5: Compare against the Task 0 baseline**

```sh
kubectl --context local-k3s get svc -A -o json | jq -r '.items[]|select(.spec.type=="LoadBalancer")|"\(.metadata.namespace)/\(.metadata.name) \((.status.loadBalancer.ingress//[])|map(.ip)|join(","))"' | sort > /tmp/after-lb.txt
diff <(sort /tmp/baseline-lb.txt) /tmp/after-lb.txt && echo "LB IPs unchanged"
```

Expected: `LB IPs unchanged`.

- [ ] **Step 6: Delete the test snapshot**

```sh
./proxmox/snapshot-cluster.sh delete rollbacktest
```

- [ ] **Step 7: Verify the committed documentation**

The **Cluster snapshots** section of `proxmox/README.md` was written and
committed in Phase A (`0526b27`). **Do not rewrite it.** Confirm it is present
and unmodified:

```sh
git status --porcelain -- proxmox/README.md
git log --oneline -1 -- proxmox/README.md
grep -n "^## Cluster snapshots" proxmox/README.md
```

Expected: no `git status` output, most recent commit `0526b27`, and the heading
found.

Then read that section and check it still matches what you just observed —
`create`'s pre-flight, the label rules, `rollback`'s all-or-nothing behaviour.
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

- [ ] **Step 2: Extend the LV and grow the filesystem**

`ext4` supports online resize, so no downtime.

```sh
PW=$(tr -d '\n' < ~/sudo-pw.txt)
printf '%s\n' "$PW" | ssh -o BatchMode=yes grant@k3s-controller \
  'sudo -S -p "" sh -c "lvextend -l +100%FREE -r /dev/ubuntu-vg/ubuntu-lv"' 2>/dev/null
```

Expected: `Size of logical volume ubuntu-vg/ubuntu-lv changed from 31.00 GiB ... to <61.x> GiB` and `The filesystem on /dev/mapper/ubuntu--vg-ubuntu--lv is now ... blocks long.`

- [ ] **Step 3: Verify the post-change state**

```sh
PW=$(tr -d '\n' < ~/sudo-pw.txt)
printf '%s\n' "$PW" | ssh -o BatchMode=yes grant@k3s-controller 'sudo -S -p "" df -h /' 2>/dev/null
kubectl --context local-k3s get nodes k3s-controller
```

Expected: `/` ≈ 61G with ~39G available and usage ~34%; node still `Ready,SchedulingDisabled`.

**Rollback trigger:** `lvextend` errors, or `/` becomes read-only. LV extension is not reversible in place — roll back the VM snapshot from Task 4's gate if this is run after one, otherwise restore from the Task 2 procedure.

---

## Task 4: Widen the Velero schedule

Velero currently backs up 6 of ~31 namespaces, omitting `monitoring`, `truelist-staging` (MariaDB + Redis), `infra`, `unifi`, `wprb-rocks`, `pmbot` and `cert-manager`. This is a second safety net behind snapshots, not a replacement.

**Files:** none — `velero/values.yml` was updated and committed in Phase A. This
task verifies it, then applies it to the cluster.

- [ ] **Step 1: Record the pre-change state**

```sh
kubectl --context local-k3s get schedules.velero.io velero-homelab-daily -n velero -o jsonpath='{.spec.template.includedNamespaces}'; echo
```

Expected: `["home-assistant","dev-box","openclaw","openclaw-dottie","openclaw-stonk","immich"]`

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

```sh
helm upgrade velero vmware-tanzu/velero --kube-context local-k3s -n velero \
  --version 12.1.0 -f velero/values.yml
```

Expected: `STATUS: deployed`, `REVISION: 3`.

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

Expected: `PHASE=Completed`, `ERR=<none>`, and `ITEMS` substantially higher than the previous 466 (now covering ~25 namespaces).

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

**Rollback trigger:** backup phase is `Failed` (not `PartiallyFailed`), or runtime exceeds 60 minutes. Recovery: `helm rollback velero 2 -n velero`.

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

**Rollback trigger:** a workload that was `Running` before stops being `Running`, or the `openclaw-stonk` data PVC changes identity. Recovery: `helm rollback <release> <previous-rev>`; if the pod cannot start, roll back the cluster snapshot.

---

## Task 6: Disable k3s ServiceLB

`k3s server` runs with no arguments, so klipper ServiceLB is enabled alongside MetalLB. It creates hostPort DaemonSets for all 5 LoadBalancer services; `svclb-lb-unifi` has been `Pending` for **650 days** because it needs 6 hostPorts (UniFi 80/443/8080/8443/8843/8880) while `svclb-traefik` already holds 80/443. `svclb-traefik` also shows 92-104 restarts.

MetalLB performs the actual IP assignment, so klipper is redundant.

**Files:** none — `k3s/config.yaml` and `k3s/README.md` were authored and
committed in Phase A. This task verifies them, then installs `config.yaml` on
the controller.

**Interfaces:**
- Produces: `--disable=servicelb` in effect; 5 `svclb-*` DaemonSets removed; hostPorts 80/443 freed on all nodes.

- [ ] **Step 1: Record the pre-change state**

```sh
kubectl --context local-k3s get ds -n kube-system | grep svclb
kubectl --context local-k3s get pods -A | grep -c svclb
cp /tmp/baseline-lb.txt /tmp/pre-servicelb-lb.txt; cat /tmp/pre-servicelb-lb.txt
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

```sh
for i in $(seq 1 20); do kubectl --context local-k3s get --raw /readyz >/dev/null 2>&1 && { echo "API ready"; break; }; sleep 10; done
kubectl --context local-k3s get ds -n kube-system | grep svclb || echo "no svclb DaemonSets"
kubectl --context local-k3s get pods -A | grep svclb || echo "no svclb pods"
```

Expected: `API ready`, then `no svclb DaemonSets` and `no svclb pods`.

- [ ] **Step 6: Verify all 5 LoadBalancer IPs survived — this is the acceptance test**

```sh
sleep 30
kubectl --context local-k3s get svc -A -o json | jq -r '.items[]|select(.spec.type=="LoadBalancer")|"\(.metadata.namespace)/\(.metadata.name) \((.status.loadBalancer.ingress//[])|map(.ip)|join(","))"' | sort > /tmp/post-servicelb-lb.txt
diff <(sort /tmp/pre-servicelb-lb.txt) /tmp/post-servicelb-lb.txt && echo "ALL 5 LB IPs UNCHANGED"
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

- [ ] **Step 9: Delete the gate snapshot**

`k3s/config.yaml` and `k3s/README.md` are already committed, so there is nothing
to stage. Confirm the tree is clean rather than attempting a commit:

```sh
git status --porcelain -- k3s/config.yaml k3s/README.md
./proxmox/snapshot-cluster.sh delete pre-servicelb
```

Expected: no `git status` output, then `OK: snapshot set 'pre-servicelb' deleted`.

**Rollback trigger:** any of the 5 LoadBalancer IPs missing or changed, traefik unreachable, or the API not ready within 200s. Recovery: `./proxmox/snapshot-cluster.sh rollback pre-servicelb`.

---

## Task 7: Diagnose the 171-day-failed certificate

`truelist-staging/truelist-stag-io-cert` has been `READY=False` for 171 days with a live `cm-acme-http-solver-sqcfr` pod. Diagnose **before** upgrading cert-manager so a pre-existing failure is not mistaken for upgrade fallout.

**Files:** none (investigation; fix may be a follow-up)

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

Recorded before the cert-manager upgrade so this pre-existing failure is not
misread as upgrade fallout.

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
solver. Documented before upgrading cert-manager so the pre-existing failure is
not misread as upgrade fallout." -- cert-manager/README.md
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
kubectl --context local-k3s get pods -n tailscale --no-headers | wc -l
kubectl --context local-k3s exec -n monitoring prometheus-kube-prometheus-stack-prometheus-0 -c prometheus -- \
  wget -qO- 'http://localhost:9090/api/v1/targets?state=active' 2>/dev/null | \
  jq -r '[.data.activeTargets[]|select(.labels.cluster=="ovh" or .labels.cluster=="production")]|group_by(.health)|.[]|"\(.[0].health): \(length)"'
```

Record the operator version, the pod count, and how many OVH/production targets are `up`. That target count is the acceptance criterion in Step 8.

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

Expected: `STATUS: deployed`, `REVISION: 2`, rollout complete.

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

```sh
sleep 180
kubectl --context local-k3s get pods -n tailscale --no-headers | grep -vc Running || echo "all running"
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

**Rollback trigger:** the `operator-oauth` Secret is missing after the upgrade, OVH/production `up` target count does not return to the Step 1 value within 5 minutes, or the operator pod crash-loops. Recovery: `helm rollback tailscale-operator 1 -n tailscale`; if that fails, `./proxmox/snapshot-cluster.sh rollback pre-tailscale`. Note that `helm rollback` will **not** bring back a pruned Secret with a new credential in it — that has to be re-created from the Tailscale admin console.

---

## Task 9: Upgrade MetalLB v0.14.5 → v0.16.1

Installed from raw manifests, not Helm. Config is minimal: one `IPAddressPool` (`192.168.20.1-255`) and one `L2Advertisement`, no BGP. **This is load-bearing** — it provides the IP for Traefik, which fronts all 6 Ingresses, plus UniFi and the registry.

**Files:** none — `metallb/README.md` was updated and committed in Phase A. This
task verifies it.

- [ ] **Step 1: Record the pre-change state**

```sh
kubectl --context local-k3s get deploy,ds -n metallb-system -o jsonpath='{range .items[*]}{.metadata.name}{"\t"}{.spec.template.spec.containers[*].image}{"\n"}{end}'
kubectl --context local-k3s get ipaddresspools,l2advertisements -A
cat /tmp/baseline-lb.txt
```

Expected: images at `v0.14.5`, pool `192.168.20.1-255`, and the 5 LB IPs.

- [ ] **Step 2: Back up the CRs (they must survive the manifest apply)**

```sh
mkdir -p /tmp/metallb-backup
kubectl --context local-k3s get ipaddresspools,l2advertisements -n metallb-system -o yaml > /tmp/metallb-backup/crs.yaml
grep -c "kind:" /tmp/metallb-backup/crs.yaml
```

Expected: at least 2.

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
sleep 30
kubectl --context local-k3s get svc -A -o json | jq -r '.items[]|select(.spec.type=="LoadBalancer")|"\(.metadata.namespace)/\(.metadata.name) \((.status.loadBalancer.ingress//[])|map(.ip)|join(","))"' | sort > /tmp/post-metallb-lb.txt
diff <(sort /tmp/baseline-lb.txt) /tmp/post-metallb-lb.txt && echo "ALL 5 LB IPs UNCHANGED"
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

**Rollback trigger:** any LB IP missing/changed, or any of the 3 probed IPs unreachable after 2 minutes. Recovery: re-apply the v0.14.5 manifest and `kubectl apply -f /tmp/metallb-backup/crs.yaml`; if IPs still do not return, `./proxmox/snapshot-cluster.sh rollback pre-metallb`.

---

## Task 10: Upgrade cert-manager v1.14.5 → v1.17.2 (intermediate)

A 7-minor jump in one step gives a large surface to bisect. Stopping at 1.17 first halves it. cert-manager's webhook is cluster-wide — if it breaks, **all** Certificate/Issuer admission fails.

**Files:** none. `cert-manager/install-crd.sh` already targets the **final**
version (`v1.21.2`, committed in Phase A as `3f1ae20`) and is deliberately not
stepped down to the intermediate. **Do not run `install-crd.sh` in this task** —
it would jump straight to v1.21.2 and skip the intermediate stop this task exists
to provide. Use the explicit commands in Steps 3 and 4.

- [ ] **Step 1: Record the pre-change state**

```sh
kubectl --context local-k3s get deploy -n cert-manager -o jsonpath='{range .items[*]}{.metadata.name}{"\t"}{.spec.template.spec.containers[0].image}{"\n"}{end}'
kubectl --context local-k3s get clusterissuers,certificates -A
```

Expected: images at `v1.14.5`; `letsencrypt-prod` `True`; 5 Certificates, 4 `True` and `truelist-stag-io-cert` `False` (known, Task 7).

- [ ] **Step 2: Gate — snapshot**

```sh
./proxmox/snapshot-cluster.sh create pre-certmanager-117
```

- [ ] **Step 3: Apply CRDs first**

cert-manager requires CRDs to be updated **before** the chart.

```sh
kubectl --context local-k3s apply -f https://github.com/cert-manager/cert-manager/releases/download/v1.17.2/cert-manager.crds.yaml
kubectl --context local-k3s get crd | grep cert-manager
```

Expected: 6 cert-manager CRDs `configured`.

- [ ] **Step 4: Upgrade the chart**

`installCRDs` was renamed to `crds.enabled` in cert-manager 1.15; the old key still works but is deprecated. Since CRDs were applied in Step 3, disable chart-managed CRDs.

```sh
helm repo add jetstack https://charts.jetstack.io 2>/dev/null; helm repo update jetstack >/dev/null
helm upgrade cert-manager jetstack/cert-manager --kube-context local-k3s \
  -n cert-manager --version v1.17.2 \
  --set crds.enabled=false --set crds.keep=true \
  --wait --timeout 10m
```

Expected: `STATUS: deployed`.

- [ ] **Step 5: Verify the webhook is serving — the critical check**

```sh
kubectl --context local-k3s get pods -n cert-manager
kubectl --context local-k3s rollout status deploy/cert-manager-webhook -n cert-manager --timeout=5m
```

Then prove admission works by creating and deleting a throwaway Issuer. A broken webhook makes this fail:

```sh
kubectl --context local-k3s apply -f - <<'EOF'
apiVersion: cert-manager.io/v1
kind: Issuer
metadata:
  name: webhook-probe
  namespace: default
spec:
  selfSigned: {}
EOF
kubectl --context local-k3s get issuer webhook-probe -n default
kubectl --context local-k3s delete issuer webhook-probe -n default
```

Expected: `issuer.cert-manager.io/webhook-probe created`, then deleted. Failure here means the webhook is broken — **roll back immediately**.

- [ ] **Step 6: Verify existing certificates still reconcile**

```sh
sleep 60
kubectl --context local-k3s get certificates -A
kubectl --context local-k3s get clusterissuer letsencrypt-prod
```

Expected: the same 4 `True` certificates as Step 1, `letsencrypt-prod` still `True`. No certificate that was `True` may become `False`.

**Rollback trigger:** the webhook probe fails, any previously-`True` certificate goes `False`, or `letsencrypt-prod` stops being `Ready`. Recovery: `helm rollback cert-manager 1 -n cert-manager`; CRDs are kept (`crds.keep=true`) so they are not destroyed. If the webhook is still broken, `./proxmox/snapshot-cluster.sh rollback pre-certmanager-117`.

---

## Task 11: Upgrade cert-manager v1.17.2 → v1.21.2

**Files:** none — `cert-manager/install-crd.sh` and `cert-manager/README.md` were
updated and committed in Phase A. This task verifies them, then applies the
upgrade. (Task 7 Step 4 does append a "Known issues" section to
`cert-manager/README.md`; that is the one cert-manager file change Phase B
still makes, and Task 7 commits it.)

- [ ] **Step 1: Confirm Task 10 landed cleanly**

```sh
kubectl --context local-k3s get deploy -n cert-manager -o jsonpath='{range .items[*]}{.metadata.name}{"\t"}{.spec.template.spec.containers[0].image}{"\n"}{end}'
kubectl --context local-k3s get certificates -A
```

Expected: images at `v1.17.2`, same 4 `True` certificates.

- [ ] **Step 2: Gate — snapshot**

```sh
./proxmox/snapshot-cluster.sh create pre-certmanager-121
```

- [ ] **Step 3: Apply CRDs, then upgrade**

```sh
kubectl --context local-k3s apply -f https://github.com/cert-manager/cert-manager/releases/download/v1.21.2/cert-manager.crds.yaml
helm upgrade cert-manager jetstack/cert-manager --kube-context local-k3s \
  -n cert-manager --version v1.21.2 \
  --set crds.enabled=false --set crds.keep=true \
  --wait --timeout 10m
```

- [ ] **Step 4: Repeat the webhook admission probe**

```sh
kubectl --context local-k3s rollout status deploy/cert-manager-webhook -n cert-manager --timeout=5m
kubectl --context local-k3s apply -f - <<'EOF'
apiVersion: cert-manager.io/v1
kind: Issuer
metadata:
  name: webhook-probe
  namespace: default
spec:
  selfSigned: {}
EOF
kubectl --context local-k3s get issuer webhook-probe -n default
kubectl --context local-k3s delete issuer webhook-probe -n default
```

Expected: created then deleted.

- [ ] **Step 5: Verify certificates, then verify the committed cert-manager files**

```sh
sleep 60
kubectl --context local-k3s get certificates -A
```

Expected: same 4 `True`.

`cert-manager/install-crd.sh` and `cert-manager/README.md` were both updated and
committed in Phase A (`3f1ae20`). **Do not rewrite either.** Confirm they are
present, unmodified, and consistent with what Step 3 just applied:

```sh
git status --porcelain -- cert-manager/install-crd.sh cert-manager/README.md
git log --oneline -1 -- cert-manager/install-crd.sh cert-manager/README.md
grep -n "v1\.21\.2\|crds\.enabled\|crds\.keep" cert-manager/install-crd.sh
grep -n "v1\.21\.2\|install-crd.sh" cert-manager/README.md
```

Expected: no `git status` output, most recent commit `3f1ae20`, the script
pinning `v1.21.2` with `crds.enabled=false` and `crds.keep=true`, and the README
attributing that version to `install-crd.sh` rather than asserting what is live
in the cluster. That distinction is deliberate — prose that claims a running
version goes stale silently. If you want the cluster's actual version, read it:

```sh
kubectl --context local-k3s get deploy cert-manager -n cert-manager \
  -o jsonpath='{.spec.template.spec.containers[0].image}'; echo
```

- [ ] **Step 6: Delete both gate snapshots**

Nothing to stage — both files are already committed. Confirm the tree is clean
rather than attempting a commit:

```sh
git status --porcelain -- cert-manager/install-crd.sh cert-manager/README.md
./proxmox/snapshot-cluster.sh delete pre-certmanager-117
./proxmox/snapshot-cluster.sh delete pre-certmanager-121
```

Expected: no `git status` output, then both snapshot sets deleted.

**Rollback trigger:** same as Task 10. Recovery: `helm rollback cert-manager <prev-rev> -n cert-manager`, else snapshot rollback.

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
  kubectl --context local-k3s drain $h --ignore-daemonsets --delete-emptydir-data --timeout=300s || true
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
kubectl --context local-k3s get nodes -o wide
kubectl --context local-k3s get svc -A -o json | jq -r '.items[]|select(.spec.type=="LoadBalancer")|"\(.metadata.namespace)/\(.metadata.name) \((.status.loadBalancer.ingress//[])|map(.ip)|join(","))"' | sort > /tmp/post-k3s-lb.txt
diff <(sort /tmp/baseline-lb.txt) /tmp/post-k3s-lb.txt && echo "ALL 5 LB IPs UNCHANGED"
kubectl --context local-k3s get ingress -A
kubectl --context local-k3s get certificates -A
kubectl --context local-k3s get pods -A --field-selector=status.phase!=Succeeded | grep -vE "Running|Completed" || echo "no unhealthy pods"
curl -s -o /dev/null -w "traefik -> HTTP %{http_code}\n" --max-time 10 http://192.168.20.1/
```

Expected: all 4 nodes `v1.29.15+k3s1` and `Ready`; `ALL 5 LB IPs UNCHANGED`; 6 Ingresses on `192.168.20.1`; the same 4 `True` certificates; `no unhealthy pods`; traefik answering.

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
- Tailscale operator, MetalLB and cert-manager are current
- k3s is patch-current on 1.29 and the upgrade mechanism is validated
- klipper/MetalLB conflict resolved, clearing 5 of the 6 firing warning groups
- Velero covers ~25 namespaces instead of 6
- Two secrets (Slack webhook, Tailscale OAuth) are out of Helm values
- k3s configuration is declarative and survives reinstalls

**Explicitly NOT done:** Kubernetes minor hops 1.30 → 1.36. Before planning those, re-sample `apiserver_requested_deprecated_apis` after ≥7 days of API server uptime — the earlier reading was taken after only 2.5h and proves nothing. The open question of in-place hops versus a rebuild at 1.36 is recorded in the spec.
