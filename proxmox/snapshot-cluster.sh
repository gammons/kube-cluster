#!/usr/bin/env bash
# Snapshot/rollback the whole k3s cluster as one unit.
#
# Covers everything: the k3s SQLite datastore and all 23 local-path PVCs live on
# the VM zvols; all 15 nfs PVCs live on main-pool/k3s-nfs.
#
# Velero is NOT a substitute -- it covers 6 of ~31 namespaces.
#
# A snapshot "set" is one label applied to 5 targets: the 4 VMs and the NFS
# dataset. create and rollback both survey all 5 targets before touching
# anything, because a set that only partly exists is worse than no set at all --
# rolling back 3 of 4 VMs leaves the cluster inconsistent with the NFS dataset
# and every VM powered off, which is not something to discover mid-incident.
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

r() { ssh -o BatchMode=yes "$PVE" "$@"; }

# Snapshot names on VM $1, one per line. qm listsnapshot draws a tree:
#   `-> pre-metallb          2026-09-13 07:00:00     cluster gate pre-metallb
#    `-> current                                     You are here!
# so strip the indent and the "`->" marker, then keep the first field.
# "current" is the live state, not a snapshot, so drop it.
vm_snapshots() {
  r "qm listsnapshot $1" \
    | sed -e 's/^[[:space:]`]*//' -e 's/^->[[:space:]]*//' \
    | awk 'NF > 0 && $1 != "current" { print $1 }'
}

# Snapshot names on the NFS dataset, one per line. The sed matches only
# "<dataset>@", so snapshots of any child dataset are ignored.
dataset_snapshots() {
  r "zfs list -t snapshot -H -o name -r ${DATASET}" \
    | sed -n "s|^${DATASET}@||p"
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
    r "zpool status -x" | grep -q "all pools are healthy" || { echo "ABORT: a pool is unhealthy"; exit 1; }
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
    for id in $VMS; do
      echo "-- VM $id"
      r "qm snapshot $id $LABEL --description 'cluster gate $LABEL'"
    done
    echo "== snapshotting NFS dataset =="
    r "zfs snapshot ${DATASET}@${LABEL}"
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
    waited=0
    while :; do
      RUNNING=""
      for id in $VMS; do
        state=$(vm_status "$id") \
          || { echo "ABORT: cannot read status of VM $id; nothing rolled back" >&2; exit 1; }
        [ "$state" = "stopped" ] || RUNNING="$RUNNING vm$id(${state:-unknown})"
      done
      [ -n "$RUNNING" ] || break
      [ "$waited" -lt "$STOP_WAIT" ] || {
        echo "ABORT: still not stopped after ${STOP_WAIT}s:$RUNNING" >&2
        echo "       Nothing has been rolled back. Stop those VMs by hand, then" >&2
        echo "       re-run; rolling back a running VM fails mid-set." >&2
        exit 1
      }
      sleep 3
      waited=$((waited + 3))
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
    r "zfs rollback -r ${DATASET}@${LABEL}" || {
      echo "FAILED: all 4 VMs rolled back but ${DATASET} did NOT." >&2
      echo "        The nfs PVCs are still newer than the VMs. Leave the VMs stopped," >&2
      echo "        and re-run rollback '$LABEL' once the dataset can be rolled back." >&2
      exit 1
    }
    r "systemctl restart nfs-server"
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
