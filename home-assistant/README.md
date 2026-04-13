# Home assistant helm chart

Create via:
```
helm repo add pajikos http://pajikos.github.io/home-assistant-helm-chart/
helm repo update
helm install home-assistant pajikos/home-assistant --values values.yml -n home-assistant
```

Delete via:
```
helm uninstall -n home-assistant -f values.yml
```

## Troubleshooting

### Pod stuck in ContainerCreating with "already mounted or mount point busy" (2026-04-11)

**Symptoms:** `home-assistant-0` stuck in `ContainerCreating` for 15+ hours. Events show:
```
MountVolume.MountDevice failed for volume "pvc-..." : mount failed: exit status 32
mount: ... /dev/longhorn/pvc-... already mounted or mount point busy.
```

**Root cause:** `multipathd` on the node created a device-mapper mapping (`mpatha`/`dm-1`) on top of the Longhorn iSCSI block device. This held the device "busy" and prevented the CSI driver from mounting it. The ext4 filesystem itself also had minor errors (corrupt block bitmap from an earlier EIO) but those alone did not block the mount.

**Fix:**

1. Remove the multipath mapping on the affected node:
   ```bash
   # Via kubectl debug pod on the node:
   kubectl debug node/<node> -it --image=ubuntu --profile=sysadmin -- \
     chroot /host dmsetup remove mpatha
   ```

2. Delete the pod so the StatefulSet recreates it:
   ```bash
   kubectl delete pod home-assistant-0 -n home-assistant
   ```

3. If the Longhorn volume is stuck in a bad state (`creating`/`faulted`), you may need to:
   - Scale down the StatefulSet: `kubectl scale statefulset home-assistant -n home-assistant --replicas=0`
   - Delete orphaned backup CRs and snapshot CRs in `longhorn-system` that create attachment tickets preventing detach
   - Clear attachment tickets: `kubectl patch volumeattachments.longhorn.io <vol> -n longhorn-system --type=json -p '[{"op":"replace","path":"/spec/attachmentTickets","value":{}}]'`
   - Force volume status to `detached` if stuck in `creating`: `kubectl patch volumes.longhorn.io <vol> -n longhorn-system --type=merge --subresource=status -p '{"status":{"state":"detached","robustness":"unknown","currentNodeID":""}}'`
   - Scale back up: `kubectl scale statefulset home-assistant -n home-assistant --replicas=1`

**Prevention:** A multipath blacklist was added to `/etc/multipath.conf` on all worker nodes (k3s-node-1, k3s-node-2, k3s-node-3) to prevent `multipathd` from grabbing Longhorn iSCSI devices:
```
blacklist {
    device {
        vendor "IET"
        product "VIRTUAL-DISK"
    }
}
```
If a node is reprovisioned, this config must be reapplied.

### Notes

- The Longhorn volume replica count was reduced from 3 to 1 during the incident. Consider scaling back to 2+ for redundancy:
  ```bash
  kubectl patch volumes.longhorn.io <vol> -n longhorn-system --type=merge -p '{"spec":{"numberOfReplicas":2}}'
  ```
