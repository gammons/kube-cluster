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

## Tailnet access

| Path | URL | Works from |
|------|-----|------------|
| Tailnet | `http://home-assistant.rya-scala.ts.net:8124` | any device running Tailscale with MagicDNS, at home or away |
| LAN | `http://<node-ip>:30285` | the home network only (Helm chart's NodePort) |

`tailscale-service.yml` is a separate ClusterIP Service, not part of the Helm values, so it
is applied with kubectl and does not need the broken `helm upgrade`:
```bash
kubectl --context local-k3s apply -f tailscale-service.yml
```

The Tailscale operator runs an L4 proxy pod for it in the `tailscale` namespace. Traffic is
passed through unmodified (no `X-Forwarded-For`), so HA needs no `http:` /
`trusted_proxies` config. WireGuard encrypts it, so plain HTTP is fine.

Verify:
```bash
kubectl --context local-k3s -n tailscale get pods \
  -l tailscale.com/parent-resource=home-assistant-tailscale
curl -fsS -o /dev/null -w '%{http_code}\n' http://home-assistant.rya-scala.ts.net:8124/
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

## Emporia Vue energy monitor (2026-09-30)

The Emporia Vue at `192.168.1.193` runs stock Emporia firmware, which has no local API (ports 80/443/6053 are all closed), so it is integrated through Emporia's cloud.

**Uses the [`emporia_vue`](https://github.com/magico13/ha-emporia-vue) custom component (v0.12.3).** Data is polled from the Emporia cloud and is at best 1-minute resolution.

**Setup pieces (on the PVC, `/config` in the pod):**

- `custom_components/emporia_vue/` — installed manually (no HACS): download the release tarball, then
  `tar cf - -C <repo>/custom_components emporia_vue | kubectl exec -i -n home-assistant home-assistant-0 -- tar xf - -C /config/custom_components/`
- `emporia_vue-0.9.3.bak.tar` — the old, never-configured v0.9.3 copy that was replaced. Keep backups outside `custom_components/`, because a second directory with the same `emporia_vue` domain would conflict.
- Config entry — added via Settings → Devices & Services → Add Integration → Emporia Vue, using the Emporia app email/password. Credentials live in the HA config entry, not in this repo or `secrets.yaml`.

**Notes:**

- v0.12.x requires `boto3>=1.37.1,<1.43.0`; HA 2026.4.2 ships boto3 1.37.1. Before upgrading HA or this component, check the new `manifest.json` requirements against the container: `kubectl exec -n home-assistant home-assistant-0 -- pip show boto3`.
- Restarting HA to pick up a new custom component: `kubectl exec -n home-assistant home-assistant-0 -- kill -TERM <pid of "python3 -m homeassistant">`. The s6 finish script halts the container, and kubelet restarts it in the same pod (same node, volume stays attached), which avoids the Longhorn/Zigbee issues of deleting the pod.
- The device is a Vue 2 (`VUE002`).
- Local alternative: Vue 2/3 can be serial-flashed with ESPHome ([emporia-vue-local/esphome](https://github.com/emporia-vue-local/esphome)) for ~1s local updates, at the cost of the Emporia app/cloud and a community-maintained component.

### Energy panel and Power dashboard

Both are built on the Emporia sensors and live in HA's `.storage` (UI-managed). Copies are kept in this repo for restore:

- `energy-prefs.json` — Energy panel settings (Settings → Dashboards → Energy). Grid = `sensor.main_energy_today` at a fixed $0.0809/kWh, with `sensor.main_power_minute_average` as live power. Each of the 16 circuits is an individual device (`sensor.<circuit>_energy_today` + `_power_minute_average`). `Balance` is deliberately excluded, because the panel computes untracked usage itself.
- `power-dashboard.yaml` — the "Power" sidebar dashboard (URL `/power-monitor`; HA requires a hyphen in dashboard URL paths). It uses only core cards (no HACS). The power-flow and usage-history cards read from the Energy panel settings, so they break if those are removed.
- Cost is tracked by `sensor.main_energy_today_cost`, which HA creates from the grid price. It only counts from when the price was set (2026-09-30).

**To restore or change:**

- Dashboard: open it → ✏️ → ⋮ → Raw configuration editor, and paste `power-dashboard.yaml`. If the dashboard itself is gone, first recreate it in Settings → Dashboards with URL `power-monitor`.
- Energy settings: re-enter them in the UI from `energy-prefs.json`, or send it over the websocket API as `{"type": "energy/save_prefs", ...contents}`. The API needs a long-lived access token (Profile → Security); revoke the token afterwards.

### Notes

- The Longhorn volume replica count was reduced from 3 to 1 during the 2026-04-11 incident. Consider scaling back to 2+ for redundancy:
  ```bash
  kubectl patch volumes.longhorn.io <vol> -n longhorn-system --type=merge -p '{"spec":{"numberOfReplicas":2}}'
  ```
