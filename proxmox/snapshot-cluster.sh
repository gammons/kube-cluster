#!/usr/bin/env bash
# Snapshot/rollback the whole k3s cluster as one unit.
#
# Covers everything: the k3s SQLite datastore and all 23 local-path PVCs live on
# the VM zvols; all 15 nfs PVCs live on main-pool/k3s-nfs.
#
# Velero is NOT a substitute. Even once its schedule is widened to ~25 of the ~31
# namespaces it covers NAMESPACED OBJECTS ONLY -- `includeClusterResources` is
# unset, so PVs, StorageClasses, CRDs and ClusterIssuers are not in a Velero
# backup at all. This script is the only thing that covers them.
#
# A snapshot "set" is one label applied to 5 targets: the 4 VMs and the NFS
# dataset. create and rollback both survey all 5 targets before touching
# anything, because a set that only partly exists is worse than no set at all --
# rolling back 3 of 4 VMs leaves the cluster inconsistent with the NFS dataset
# and every VM powered off, which is not something to discover mid-incident.
#
# ONLY THE MOST RECENT SET CAN BE ROLLED BACK TO. Proxmox refuses to roll a
# zvol back to anything but its newest snapshot, so rollback checks recency as
# well as presence, on all 5 targets, before it stops anything. Hold at most one
# gate at a time; if you need an older one, delete the newer sets first and
# accept that you are discarding the route back past them.
#
# Usage: snapshot-cluster.sh {create|list|rollback|delete} <label>
set -euo pipefail

PVE=root@192.168.5.1
VMS="100 101 102 103"
DATASET=main-pool/k3s-nfs
STOP_WAIT=60            # seconds to wait for VMs to actually reach 'stopped'
ACTION=${1:-}
LABEL=${2:-}

usage() { echo "usage: $0 {create|list|rollback|delete} <label>" >&2; exit 2; }
[ -n "$ACTION" ] || usage
case "$ACTION" in list) ;; *) [ -n "$LABEL" ] || usage ;; esac
# The label is interpolated into remote shell commands, and into argv positions
# where a leading '-' would be read as an option ("qm snapshot 100 -foo ...").
# Proxmox itself requires ^[A-Za-z][A-Za-z0-9_-]+$, so a leading '-' can only
# ever fail -- reject it here, with a message that says why.
case "$LABEL" in
  *[!a-zA-Z0-9_-]*) echo "label must be [a-zA-Z0-9_-]" >&2; exit 2 ;;
  -*) echo "label must not start with '-' (it would be parsed as an option)" >&2; exit 2 ;;
esac

# ConnectTimeout is not optional. rollback's stop-wait loop runs 4 of these per
# iteration, and it runs at the one moment the host is least likely to answer:
# all 4 VMs already stopped, mid-incident. Without it a half-open TCP connection
# hangs for the kernel's SYN retry budget (~2min each), so a "60s" bound becomes
# hours with every VM powered off. 10s is generous for a LAN host.
r() { ssh -o BatchMode=yes -o ConnectTimeout=10 "$PVE" "$@"; }

# One row per snapshot on VM $1: "<name><TAB><YYYY-MM-DD HH:MM:SS>".
# qm listsnapshot draws a tree:
#   `-> pre-metallb          2026-09-13 07:00:00     cluster gate pre-metallb
#    `-> current                                     You are here!
# so strip the indent and the "`->" marker. "current" is the live state, not a
# snapshot, so drop it.
#
# The timestamp column comes from strftime("%F %H:%M:%S") in
# PVE::GuestHelpers::print_snapshot_tree, so it is fixed-width and zero-padded
# and orders correctly under a plain string compare. A snapshot carrying no
# snaptime yields an EMPTY second field; callers must treat that as
# "unorderable", never as "old" -- see vm_blocking_snapshots.
vm_snapshot_rows() {
  r "qm listsnapshot $1" \
    | sed -e 's/^[[:space:]`]*//' -e 's/^->[[:space:]]*//' \
    | awk 'NF > 0 && $1 != "current" {
             ts = ""
             if ($2 ~ /^[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]$/ &&
                 $3 ~ /^[0-9][0-9]:[0-9][0-9]:[0-9][0-9]$/) ts = $2 " " $3
             print $1 "\t" ts
           }'
}

# Snapshot names on VM $1, one per line. A pipeline, so under `pipefail` an ssh
# failure upstream still fails the whole thing -- survey() depends on that.
vm_snapshots() { vm_snapshot_rows "$1" | cut -f1; }

# Snapshots on VM $1 that block a rollback to label $2, one per line. Empty
# output means $2 is the most recent snapshot on that VM. Returns 2 if the VM
# could not be queried at all, 3 if $2 is not on it.
#
# WHY THIS EXISTS. The VM disks live on `zfspool` storage, and PVE refuses to
# roll a zvol back to anything but its newest snapshot:
#
#   die "can't rollback, '$snap' is not most recent snapshot on '$volid'\n"
#     -- PVE/Storage/ZFSPoolPlugin.pm, volume_rollback_is_possible()
#
# Surveying for label *presence* alone is therefore not enough. With a newer
# snapshot present the pre-flight used to pass, the script stopped all 4 VMs,
# and then died on the first `qm rollback` -- leaving the whole cluster powered
# off with nothing rolled back, during an incident. That is strictly worse than
# refusing to start.
#
# ">=" rather than ">" is deliberate. A snapshot sharing $2's second cannot be
# ordered against it from this output, and PVE orders by ZFS creation time,
# which is not visible here. Reporting it costs the operator one `delete`; not
# reporting it costs the outage above. An unparseable/absent timestamp is
# reported for the same reason, as is *every* other snapshot when $2's own
# timestamp is unreadable. This guard fails closed.
#
# Known limitation: the timestamps are rendered in the Proxmox host's local
# time, so during a DST fall-back fold an hour of snapshots can compare in the
# wrong order. PVE's own check still catches that case -- the cost is that this
# pre-flight degrades to the old behaviour for that one hour a year, not that it
# lets something new through.
vm_blocking_snapshots() {
  local rows
  rows=$(vm_snapshot_rows "$1") || return 2
  printf '%s\n' "$rows" | awk -F'\t' -v want="$2" '
    { n++; name[n] = $1; ts[n] = $2; if ($1 == want) { found = 1; wantts = $2 } }
    END {
      if (!found) exit 3
      for (i = 1; i <= n; i++) {
        if (name[i] == want) continue
        # Concatenating "" forces a string compare. These timestamps are never
        # numeric, but do not leave that to awk strnum coercion rules.
        if (ts[i] == "" || ("" ts[i]) >= ("" wantts)) print name[i]
      }
    }'
}

# Snapshot names on the NFS dataset, one per line, OLDEST FIRST. The sed matches
# only "<dataset>@", so snapshots of any child dataset are ignored.
#
# `-s creation` is load-bearing, not cosmetic: dataset_blocking_snapshots()
# reads recency straight out of this order. zfs's default sort is by name, which
# would make "newer" mean "alphabetically later".
dataset_snapshots() {
  r "zfs list -t snapshot -H -o name -s creation -r ${DATASET}" \
    | sed -n "s|^${DATASET}@||p"
}

# Snapshots of the NFS dataset newer than label $1, one per line. Returns 2 if
# the dataset could not be queried, 3 if $1 is not on it.
#
# The VM half above refuses when newer snapshots exist. This half must refuse
# too, and for the opposite reason: `zfs rollback -r` does NOT refuse -- the -r
# means "destroy everything newer". Left unchecked, the two halves of a set that
# is documented to move as one unit behave differently in exactly the same
# situation: the VMs abort, the dataset silently discards snapshots. Refuse on
# both, and let the operator delete deliberately.
dataset_blocking_snapshots() {
  local snaps
  snaps=$(dataset_snapshots) || return 2
  printf '%s\n' "$snaps" | awk -v want="$1" '
    { n++; name[n] = $0; if ($0 == want) found = n }
    END {
      if (!found) exit 3
      for (i = found + 1; i <= n; i++) print name[i]
    }'
}

# Power state of VM $1, e.g. "stopped" or "running". qm status prints one line,
# "status: stopped". No 'exit' in the awk: it must drain stdin so it can never
# SIGPIPE the ssh upstream, which under pipefail would look like a query error.
vm_status() {
  r "qm status $1" | awk '/^status:/ { print $2 }'
}

# True if the newline-separated list $1 contains exactly $2. Whole-line match on
# purpose: 'pre-metallb' must not match 'pre-metallb-2'.
contains_line() {
  local line
  while IFS= read -r line; do
    if [ "$line" = "$2" ]; then return 0; fi
  done <<<"$1"
  return 1
}

# Which of the 5 targets already hold label $1: sets PRESENT and MISSING to
# space-separated target lists. create, rollback and delete all share this, so
# their idea of "the set exists" cannot drift apart. Aborts if a target cannot
# be queried at all -- an unqueryable target is not a target we may assume about.
PRESENT=""
MISSING=""
survey() {
  local id snaps
  PRESENT=""
  MISSING=""
  for id in $VMS; do
    snaps=$(vm_snapshots "$id") \
      || { echo "ABORT: cannot list snapshots of VM $id" >&2; exit 1; }
    if contains_line "$snaps" "$1"; then PRESENT="$PRESENT vm$id"; else MISSING="$MISSING vm$id"; fi
  done
  snaps=$(dataset_snapshots) \
    || { echo "ABORT: cannot list snapshots of ${DATASET}" >&2; exit 1; }
  if contains_line "$snaps" "$1"; then PRESENT="$PRESENT ${DATASET}"; else MISSING="$MISSING ${DATASET}"; fi
}

case "$ACTION" in
  create)
    echo "== pre-flight =="
    # Captured into a variable rather than piped into grep -q. Under pipefail,
    # grep -q exits the moment it matches, which can SIGPIPE the ssh upstream and
    # fail the pipeline *because* it matched -- a spurious abort on a healthy
    # pool. Same hazard vm_status() avoids by ending in awk. Testing a variable
    # races nothing, and the message can now say which case it was: an ssh
    # failure yields an empty string, an unhealthy pool yields zpool's report.
    pools=$(r "zpool status -x") || true
    case "$pools" in
      *"all pools are healthy"*) ;;
      *) echo "ABORT: pool health check failed: $pools"; exit 1 ;;
    esac
    for id in $VMS; do
      r "qm agent $id ping" >/dev/null 2>&1 \
        || { echo "ABORT: qemu-guest-agent not responding on VM $id (snapshot would be crash-consistent)"; exit 1; }
    done
    survey "$LABEL"
    [ -z "$PRESENT" ] || {
      echo "ABORT: label '$LABEL' is already in use on:$PRESENT" >&2
      echo "       Nothing has been changed. Pick a different label, or remove the" >&2
      echo "       old set first:  $0 delete $LABEL" >&2
      exit 1
    }
    echo "OK: label '$LABEL' is free on all 4 VMs and ${DATASET}"
    echo "== snapshotting VMs (guest agent quiesces the filesystem) =="
    # A failure part way through leaves a partial set, which is not a gate: a
    # later 'rollback' would refuse it, and an operator who did not read this far
    # up the scrollback would proceed with the change believing it was covered.
    # Say so explicitly, and name the exact state, as rollback does.
    SNAPPED=""
    for id in $VMS; do
      echo "-- VM $id"
      r "qm snapshot $id $LABEL --description 'cluster gate $LABEL'" || {
        NOTSNAPPED=""
        for j in $VMS; do
          case " $SNAPPED " in *" vm$j "*) ;; *) NOTSNAPPED="$NOTSNAPPED vm$j" ;; esac
        done
        echo "FAILED: snapshot of VM $id failed. A PARTIAL set now exists:" >&2
        echo "        snapshotted:${SNAPPED:- none}" >&2
        echo "        NOT snapshotted:$NOTSNAPPED" >&2
        echo "        ${DATASET}: NOT snapshotted" >&2
        echo "        The VMs are all still running; nothing was changed." >&2
        echo "        A partial set is NOT a gate -- do not proceed with the change" >&2
        echo "        it was meant to cover. Recovery: remove the partial set with" >&2
        echo "        '$0 delete $LABEL', fix VM $id, then re-run create." >&2
        exit 1
      }
      SNAPPED="$SNAPPED vm$id"
    done
    echo "== snapshotting NFS dataset =="
    r "zfs snapshot ${DATASET}@${LABEL}" || {
      echo "FAILED: all 4 VMs are snapshotted but ${DATASET} is NOT." >&2
      echo "        The 15 nfs PVCs are outside this set, so it is not a usable" >&2
      echo "        gate. Recovery: '$0 delete $LABEL', then re-run create." >&2
      exit 1
    }
    echo "OK: snapshot set '$LABEL' created"
    ;;
  list)
    for id in $VMS; do echo "-- VM $id"; r "qm listsnapshot $id"; done
    echo "-- dataset"; r "zfs list -t snapshot -o name,used,creation -s creation ${DATASET}"
    ;;
  rollback)
    echo "== pre-flight =="
    survey "$LABEL"
    [ -z "$MISSING" ] || {
      echo "ABORT: snapshot '$LABEL' is missing on:$MISSING" >&2
      echo "       present on:${PRESENT:- nothing}" >&2
      echo "       Nothing has been changed. Rolling back only part of the set would" >&2
      echo "       leave some targets at the old state and some at the new one, with" >&2
      echo "       every VM stopped -- worse than not rolling back at all." >&2
      exit 1
    }
    echo "OK: '$LABEL' present on all 4 VMs and ${DATASET}"
    # Presence is not enough -- the label must also be the NEWEST snapshot on
    # every target. See vm_blocking_snapshots() and dataset_blocking_snapshots()
    # for why each half needs this, and why they need it for opposite reasons.
    # This runs before the confirm prompt and before anything is stopped: an
    # abort here costs nothing, an abort after the stop loop costs the cluster.
    echo "-- confirming '$LABEL' is the most recent snapshot on all 5 targets"
    BLOCKED=""
    for id in $VMS; do
      blockers=$(vm_blocking_snapshots "$id" "$LABEL") && rc=0 || rc=$?
      case "$rc" in
        0) ;;
        2) echo "ABORT: cannot list snapshots of VM $id; nothing rolled back" >&2; exit 1 ;;
        3) echo "ABORT: '$LABEL' vanished from VM $id between the survey and the" >&2
           echo "       recency check; nothing rolled back. Re-run." >&2
           exit 1 ;;
        *) echo "ABORT: recency check failed on VM $id (status $rc); nothing rolled back" >&2; exit 1 ;;
      esac
      [ -z "$blockers" ] || BLOCKED="$BLOCKED
  vm$id: $(printf '%s' "$blockers" | tr '\n' ' ')"
    done
    blockers=$(dataset_blocking_snapshots "$LABEL") && rc=0 || rc=$?
    case "$rc" in
      0) ;;
      2) echo "ABORT: cannot list snapshots of ${DATASET}; nothing rolled back" >&2; exit 1 ;;
      3) echo "ABORT: '$LABEL' vanished from ${DATASET} between the survey and the" >&2
         echo "       recency check; nothing rolled back. Re-run." >&2
         exit 1 ;;
      *) echo "ABORT: recency check failed on ${DATASET} (status $rc); nothing rolled back" >&2; exit 1 ;;
    esac
    [ -z "$blockers" ] || BLOCKED="$BLOCKED
  ${DATASET}: $(printf '%s' "$blockers" | tr '\n' ' ')"
    [ -z "$BLOCKED" ] || {
      echo "ABORT: '$LABEL' is not the most recent snapshot. Blocked by:$BLOCKED" >&2
      echo "" >&2
      echo "       Nothing has been changed and no VM has been stopped." >&2
      echo "       Proxmox refuses to roll a zvol back to anything but its newest" >&2
      echo "       snapshot, so this rollback would stop all 4 VMs and then fail on" >&2
      echo "       the first one -- cluster powered off, nothing reverted." >&2
      echo "       The dataset would not fail: 'zfs rollback -r' would DESTROY those" >&2
      echo "       newer snapshots instead. Neither outcome is one to discover" >&2
      echo "       mid-incident, so both halves refuse here." >&2
      echo "" >&2
      echo "       Decide which of the newer sets you no longer need, remove each" >&2
      echo "       with '$0 delete <label>', then re-run this rollback. Deleting a" >&2
      echo "       snapshot set discards the route back past that point -- that is" >&2
      echo "       the choice being made, so make it deliberately." >&2
      exit 1
    }
    echo "OK: '$LABEL' is the most recent snapshot on all 5 targets"
    echo "!! rollback discards ALL changes since '$LABEL' on 4 VMs and ${DATASET}"
    echo "!! VMs will be stopped, rolled back, and restarted."
    printf "type the label again to confirm: "; read -r c
    [ "$c" = "$LABEL" ] || { echo "aborted"; exit 1; }
    echo "== stopping VMs =="
    for id in $VMS; do r "qm stop $id --timeout 120" || true; done
    # The stop loop tolerates failures, so do not trust it. qm rollback against a
    # running VM fails (these snapshots carry no vmstate), which would abort the
    # rollback loop part way and produce exactly the split state the pre-flight
    # above exists to prevent. Verify, with an explicit bound.
    echo "-- confirming all VMs are stopped (up to ${STOP_WAIT}s)"
    # Bound on wall-clock, not on iterations. Counting 'sleep 3' alone would make
    # ${STOP_WAIT} a count of 20 loops rather than 60 seconds, and each loop also
    # spends up to 4 x ConnectTimeout in ssh when the host is struggling.
    started=$SECONDS
    while :; do
      RUNNING=""
      for id in $VMS; do
        state=$(vm_status "$id") \
          || { echo "ABORT: cannot read status of VM $id; nothing rolled back" >&2; exit 1; }
        [ "$state" = "stopped" ] || RUNNING="$RUNNING vm$id(${state:-unknown})"
      done
      [ -n "$RUNNING" ] || break
      [ "$((SECONDS - started))" -lt "$STOP_WAIT" ] || {
        echo "ABORT: still not stopped after ${STOP_WAIT}s:$RUNNING" >&2
        echo "       Nothing has been rolled back. Stop those VMs by hand, then" >&2
        echo "       re-run; rolling back a running VM fails mid-set." >&2
        exit 1
      }
      sleep 3
    done
    echo "OK: all 4 VMs stopped"
    echo "== rolling back =="
    ROLLED=""
    for id in $VMS; do
      echo "-- rollback VM $id"
      r "qm rollback $id $LABEL" || {
        NOTROLLED=""
        for j in $VMS; do
          case " $ROLLED " in *" vm$j "*) ;; *) NOTROLLED="$NOTROLLED vm$j" ;; esac
        done
        echo "FAILED: rollback of VM $id failed. Actual state right now:" >&2
        echo "        rolled back:${ROLLED:- none}" >&2
        echo "        NOT rolled back:$NOTROLLED" >&2
        echo "        ${DATASET}: NOT rolled back" >&2
        echo "        all 4 VMs: stopped" >&2
        echo "        Recovery: fix VM $id, then re-run rollback '$LABEL'. The" >&2
        echo "        snapshots still exist and rolling a VM back twice is harmless." >&2
        exit 1
      }
      ROLLED="$ROLLED vm$id"
    done
    # -r destroys snapshots newer than ${LABEL}. The recency pre-flight above
    # has already proved there are none, so this is a no-op kept for the case
    # where something creates one inside this window -- it is no longer the
    # thing that silently diverges from the VM half's behaviour.
    r "zfs rollback -r ${DATASET}@${LABEL}" || {
      echo "FAILED: all 4 VMs rolled back but ${DATASET} did NOT." >&2
      echo "        The nfs PVCs are still newer than the VMs. Leave the VMs stopped," >&2
      echo "        and re-run rollback '$LABEL' once the dataset can be rolled back." >&2
      exit 1
    }
    # Unguarded, this would exit 0 under -e only by luck: a failure here leaves
    # every VM stopped and the operator told nothing, right after the one command
    # in the script that cannot be undone. Clients hold NFS file handles across
    # the rollback, so without the restart the nfs PVCs fail with ESTALE.
    r "systemctl restart nfs-server" || {
      echo "FAILED: all 4 VMs and ${DATASET} rolled back, but nfs-server did not" >&2
      echo "        restart. Clients hold stale handles across a rollback, so the" >&2
      echo "        15 nfs PVCs will fail with ESTALE until it does. The rollback" >&2
      echo "        itself succeeded; all 4 VMs are still STOPPED." >&2
      echo "        Recovery: ssh $PVE 'systemctl restart nfs-server', then start" >&2
      echo "        the VMs by hand -- 'qm start 100', wait ~45s for the API, then" >&2
      echo "        'qm start 101', '102', '103'. Do not re-run rollback: the" >&2
      echo "        rollback is already done and repeating it is not the fix." >&2
      exit 1
    }
    echo "-- starting controller first"
    r "qm start 100"; sleep 45
    for id in 101 102 103; do r "qm start $id"; sleep 5; done
    echo "OK: rolled back to '$LABEL'. Verify with: kubectl --context local-k3s get nodes"
    ;;
  delete)
    # Idempotent: a target that does not have the snapshot is skipped, not an
    # error. But a target that has it and refuses to give it up (zfs hold, an
    # existing clone) is a real failure and must not be reported as success.
    survey "$LABEL"
    [ -n "$PRESENT" ] || { echo "OK: snapshot set '$LABEL' already absent"; exit 0; }
    FAILED=""
    for id in $VMS; do
      case " $PRESENT " in
        *" vm$id "*)
          echo "-- VM $id"
          r "qm delsnapshot $id $LABEL" || FAILED="$FAILED vm$id"
          ;;
      esac
    done
    case " $PRESENT " in
      *" ${DATASET} "*)
        echo "-- dataset"
        r "zfs destroy ${DATASET}@${LABEL}" || FAILED="$FAILED ${DATASET}"
        ;;
    esac
    [ -z "$FAILED" ] || {
      echo "FAILED: snapshot '$LABEL' could not be deleted on:$FAILED" >&2
      echo "        A zfs hold or an existing clone will block 'zfs destroy'." >&2
      exit 1
    }
    echo "OK: snapshot set '$LABEL' deleted"
    ;;
  *) usage ;;
esac
