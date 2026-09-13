#!/usr/bin/env bash
# Snapshot/rollback the whole k3s cluster as one unit.
#
# Covers everything: the k3s SQLite datastore and all 23 local-path PVCs live on
# the VM zvols; all 15 nfs PVCs live on main-pool/k3s-nfs.
#
# Velero is NOT a substitute -- it covers 6 of ~31 namespaces.
#
# Usage: snapshot-cluster.sh {create|list|rollback|delete} <label>
set -euo pipefail

PVE=root@192.168.5.1
VMS="100 101 102 103"
DATASET=main-pool/k3s-nfs
ACTION=${1:-}
LABEL=${2:-}

usage() { echo "usage: $0 {create|list|rollback|delete} <label>" >&2; exit 2; }
[ -n "$ACTION" ] || usage
case "$ACTION" in list) ;; *) [ -n "$LABEL" ] || usage ;; esac
case "$LABEL" in *[!a-zA-Z0-9_-]*) echo "label must be [a-zA-Z0-9_-]" >&2; exit 2 ;; esac

r() { ssh -o BatchMode=yes "$PVE" "$@"; }

case "$ACTION" in
  create)
    echo "== pre-flight =="
    r "zpool status -x" | grep -q "all pools are healthy" || { echo "ABORT: a pool is unhealthy"; exit 1; }
    for id in $VMS; do
      r "qm agent $id ping" >/dev/null 2>&1 \
        || { echo "ABORT: qemu-guest-agent not responding on VM $id (snapshot would be crash-consistent)"; exit 1; }
    done
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
    echo "!! rollback discards ALL changes since '$LABEL' on 4 VMs and ${DATASET}"
    echo "!! VMs will be stopped, rolled back, and restarted."
    printf "type the label again to confirm: "; read -r c
    [ "$c" = "$LABEL" ] || { echo "aborted"; exit 1; }
    for id in $VMS; do r "qm stop $id --timeout 120" || true; done
    for id in $VMS; do echo "-- rollback VM $id"; r "qm rollback $id $LABEL"; done
    r "zfs rollback -r ${DATASET}@${LABEL}"
    r "systemctl restart nfs-server"
    echo "-- starting controller first"
    r "qm start 100"; sleep 45
    for id in 101 102 103; do r "qm start $id"; sleep 5; done
    echo "OK: rolled back to '$LABEL'. Verify with: kubectl --context local-k3s get nodes"
    ;;
  delete)
    for id in $VMS; do r "qm delsnapshot $id $LABEL" || true; done
    r "zfs destroy ${DATASET}@${LABEL}" || true
    echo "OK: snapshot set '$LABEL' deleted"
    ;;
  *) usage ;;
esac
