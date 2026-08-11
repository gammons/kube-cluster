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

### SONOFF Zigbee dongle and node pinning

The SONOFF Zigbee 3.0 USB Dongle Plus V2 is a single physical USB device on the Proxmox host. Proxmox passes it through to all k3s node VMs, but only one VM can actually use it — multiple VMs claiming it simultaneously causes contention and the dongle becomes unresponsive on all of them.

**Current working node: k3s-node-1** (as of 2026-05-06)

After a power outage or Proxmox restart, all VMs may re-bind `cdc_acm` and create `/dev/ttyACM0`. If the ZHA integration shows "initializing" and gets stuck, the dongle is likely in contention. Try moving the pod to a different node.

**Diagnosing which node works:**

All nodes may show `/dev/ttyACM0` as present, but the real test is whether the dongle responds on serial. If ZHA is stuck initializing, try moving to another node:
```bash
kubectl patch statefulset home-assistant -n home-assistant --type=merge \
  -p '{"spec":{"template":{"spec":{"nodeSelector":{"kubernetes.io/hostname":"<node>"}}}}}'
```

**Device permissions:**

The device needs 666 permissions. A udev rule has been added to k3s-node-1 (and k3s-node-2) at `/etc/udev/rules.d/99-zigbee.rules` to persist this across reboots:
```
SUBSYSTEM=="tty", ATTRS{idVendor}=="1a86", ATTRS{idProduct}=="55d4", MODE="0666"
```

If moving to a new node, apply the udev rule and chmod:
```bash
kubectl debug node/<node> -it --image=ubuntu --profile=sysadmin -- \
  chroot /host bash -c '
    echo "SUBSYSTEM==\"tty\", ATTRS{idVendor}==\"1a86\", ATTRS{idProduct}==\"55d4\", MODE=\"0666\"" \
      > /etc/udev/rules.d/99-zigbee.rules
    udevadm control --reload-rules
    chmod 666 /dev/ttyACM0'
```

Also update `values.yml` with the new node name.

**Note:** `helm upgrade` on this chart currently fails due to an immutable StatefulSet field (related to `existingVolume`). If config changes are needed, either patch the resources directly or do a full `helm uninstall` + `helm install`.

## Envisalink alarm integration (2026-08-11)

The Honeywell panel is integrated via an Envisalink EVL-4 at `192.168.1.32`.

**Uses the [`envisalink_new`](https://github.com/ufodone/envisalink_new) custom component, NOT the core `envisalink` integration.** The core integration's library (pyenvisalink 4.7, unmaintained) fires all keypad keystrokes within ~1ms; the EVL-4 drops the final keystroke and arm/disarm fails with `Receive State Machine Timeout`. Verified by manual TPI test: the same keystrokes sent with 500ms spacing arm/disarm fine. `envisalink_new` queues commands sequentially with retry/timeout handling.

**Setup pieces (all live on the PVC, `/config` in the pod):**

- `custom_components/envisalink_new/` — installed manually (no HACS): clone the repo, then
  `tar cf - -C <repo>/custom_components envisalink_new | kubectl exec -i -n home-assistant home-assistant-0 -- tar xf - -C /config/custom_components/`
- `configuration.yaml` — contains `envisalink_new: !include envisialink.yaml`
- `envisialink.yaml` — host, zones, partitions (copy of the one in this repo)
- `secrets.yaml` — `envisalink_password` (EVL web login password) and `envisalink_code` (panel arm/disarm code)

**EVL-4 notes:**

- Port 4025 is the TPI (raw TCP API, not HTTP — browsers can't open it). Port 80 is the web config UI (HTTP basic auth).
- Only ONE TPI client connection at a time. For manual testing, scale down the statefulset first, then scale back up.
- Debug logging if needed: add `logger:` → `logs:` → `homeassistant.components.envisalink_new: debug` to configuration.yaml and restart.
- "Show keypad" option (Settings → Devices & Services → EyezOn → Configure) is set to `never` so the UI doesn't prompt for the code; the stored code is sent automatically.

### Notes

- The Longhorn volume replica count was reduced from 3 to 1 during the 2026-04-11 incident. Consider scaling back to 2+ for redundancy:
  ```bash
  kubectl patch volumes.longhorn.io <vol> -n longhorn-system --type=merge -p '{"spec":{"numberOfReplicas":2}}'
  ```
