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

### Two meters, two panels (2026-10-01)

The house has two utility meters feeding two panels. The Vue's main CTs are on **panel 1** only. Panel 2's circuits are individually clamped, with CT leads run over from panel 1. So the Emporia "main" (`sensor.main_*`) is panel 1 only, and the Emporia `Balance` channel (main − all circuits) goes strongly negative and is meaningless.

Circuit → panel, determined by regressing minute-to-minute changes in `main` against each circuit (k≈1 means main sees it, so panel 1; k≈0 means panel 2):

- **Panel 2:** Main AC, Water heater, Kitchen AC, Espresso machine (all k≈0.00), Washer/dryer (per owner; no activity to test).
- **Panel 1:** EV charger, Den + office, Fridge, Dishwasher/disposal, Master bath, Basement kit fan, Unknown (k≈0.9–1.0).
- **Untested** (no on/off activity; assumed panel 1): Master bedroom, Center hall, Downstairs bath, Deck GFI / pond light.

If a circuit is moved or a panel-2 circuit gets a CT, update the lists in `templates.yaml`.

`templates.yaml` (copy of `/config/templates.yaml`; `configuration.yaml` has `template: !include templates.yaml`) defines:

- `sensor.panel_2_power` / `sensor.panel_2_energy_today` — the sum of the panel-2 circuits.
- `sensor.whole_house_power` / `sensor.whole_house_energy_today` — panel 1 + panel 2.
- `sensor.whole_house_cost_today` — grand total cost: (panel 1 + panel 2 kWh) × $0.18.
- `sensor.house_energy_today` — whole house minus the EV charger (the EV is on panel 1, so it is subtracted from `main`; clamped at 0).
- `sensor.ev_charging_cost_today` / `sensor.house_cost_today` — EV kWh and house kWh × $0.18. EV + house = whole house.

The cost and house sensors are `state_class: total` with a `last_reset` (HA only allows `total` for monetary sensors), so HA keeps long-term statistics for them. That is what the dashboard's "this month" numbers and 30-day EV vs house charts use. Their statistics start on 2026-10-03; `sensor.ev_charger_energy_today` has history from before that. The $0.18 price is written in these templates as well as in the Energy panel. **If the PECO rate changes, update both.** Before 2026-10-03, `whole_house_cost_today` added up the Energy panel's two cost sensors instead.

The energy sums only include sources whose `last_reset` matches the newest one, so at midnight a partially-reset set of circuits is never added together (which would double count). Any source being unavailable makes the sum unavailable rather than wrong.

Reload after editing: Developer tools → YAML → Template entities, or restart HA. To restart without bouncing the container, call the `homeassistant.restart` service (UI: Settings → ⋮ → Restart).

### Energy panel and Power dashboard

Both are built on the Emporia sensors and live in HA's `.storage` (UI-managed). Copies are kept in this repo for restore:

- `energy-prefs.json` — Energy panel settings (Settings → Dashboards → Energy). Two grid sources, one per meter, both at a fixed $0.18/kWh (PECO, including distribution):
  - Grid 1 (panel 1): `sensor.main_energy_today`, live power `sensor.main_power_minute_average`.
  - Grid 2 (panel 2): `sensor.panel_2_energy_today`, live power `sensor.panel_2_power`.

  Each of the 16 circuits is an individual device (`sensor.<circuit>_energy_today` + `_power_minute_average`). Two are named for what is on them: `den_office` is "Den / Office / Fish tank" and `basement_kit_fan` is "Homelab server". `Balance` is deliberately excluded, because the panel computes untracked usage itself. If meter 2 turns out to be on a different PECO rate, change Grid 2's price.
- `power-dashboard.yaml` — the "Power" sidebar dashboard (URL `/power-monitor`; HA requires a hyphen in dashboard URL paths). Layout: three columns across the top — "Now" (whole-house gauge, panel 1, panel 2, EV), "Today" and "This month" (EV / House / Total, with a kWh row and a $ row; Total = the `whole_house_*` sensors). Below them, full-width sections with charts at half width, two per row: live power flow, the 30-day EV vs house cost and kWh charts plus the whole-house kWh chart, the last 24 hours, and usage history. Circuits are full width at the bottom. It uses only core cards (no HACS). The power-flow and usage-history cards read from the Energy panel settings, so they break if those are removed.
- Cost is tracked by `sensor.main_energy_today_cost` and `sensor.panel_2_energy_today_cost`, which HA creates from the grid prices. Cost history only starts from 2026-10-01, when the $0.18 price and Grid 2 were set. History before that used $0.0809 and panel 1 only.

**To restore or change:**

- Dashboard: open it → ✏️ → ⋮ → Raw configuration editor, and paste `power-dashboard.yaml`. If the dashboard itself is gone, first recreate it in Settings → Dashboards with URL `power-monitor`.
- Energy settings: re-enter them in the UI from `energy-prefs.json`, or send it over the websocket API as `{"type": "energy/save_prefs", ...contents}`. The API needs a long-lived access token (Profile → Security); revoke the token afterwards.

## Water meter (2026-10-01)

Water usage and leak alerts come from the `water-meter` service (see `water-meter/README.md`), which reads the meter from a Tapo C113 camera.

**Setup pieces (on the PVC, `/config` in the pod):**

- `water_meter.yaml` — a package (copy of `home-assistant/water_meter.yaml` in this repo) with one `rest:` resource polling `http://water-meter.water-meter.svc.cluster.local:8080/reading` every 30 s, and three automations.
- `configuration.yaml` — loads it with:
  ```yaml
  homeassistant:
    packages:
      water_meter: !include water_meter.yaml
  ```
  The pre-change copy is `configuration.yaml.bak-water-meter`.

**Entities:**

| Entity | Meaning |
|--------|---------|
| `sensor.water_meter_total` | Meter total in gallons (`device_class: water`, `total_increasing`); used by the Energy panel's water consumption |
| `sensor.water_flow` | Average flow over the last 5 minutes, gal/min |
| `sensor.water_continuous_flow` | Minutes since the meter last stood still for 15 minutes |
| `sensor.water_meter_status` | `ok` / `stale` / `error` |

**Alerts (to `notify.mobile_app_pixel_10_pro`):**

- `automation.water_possible_leak` — continuous flow above 120 minutes (running toilet, dripping hose).
- `automation.water_heavy_use` — flow above 1 gal/min for 20 minutes (hose left on, burst pipe).
- `automation.water_meter_unreadable` — status not `ok` for 30 minutes (camera offline, spotlight off, view blocked).

**Water dashboard and Energy panel (2026-10-01):**

- Energy panel: `sensor.water_meter_total` is the water source at a fixed **$0.024/gal**. That is the utility's
  average-bill figure ($90.86 for 3,780 gal/month), so it includes fixed charges and runs a little high; replace it
  with the real water + sewer volume rate when known (Settings → Dashboards → Energy → Water → edit). HA creates
  `sensor.water_meter_total_cost` from it. A copy of the Energy settings is in `energy-prefs.json`.
- `water-dashboard.yaml` — the "Water" sidebar dashboard (URL `/water-monitor`), core cards only: flow gauge,
  usage and cost today/this month, gallons per day (30 days) and per hour (2 days) bar charts from long-term
  statistics, a 24 h flow / continuous-flow history, the three alert toggles, the live camera, and the reader's
  annotated frame.
  Restore it like the Power dashboard (Raw configuration editor).
- `camera.water_meter_camera` — live view of the Tapo C113, added in the UI as a Generic Camera
  (2026-10-03) with stream source `rtsp://camera:<password>@192.168.1.48:554/stream1` and no still
  image URL (the Tapo has none; HA takes stills from the stream). The camera account is the
  one in the `water-meter-rtsp` secret. The preview and live view need HA's URLs set
  (Settings → System → Network): local `http://192.168.10.2:30285`, internet
  `http://home-assistant.rya-scala.ts.net:8124`. Without them HA hands the browser the pod's
  own address (`10.42.x.x:8123`), which it can't reach, and the video just spins.
- `image.water_meter_camera` — a trigger-based template image in `water_meter.yaml` that fetches the reader's
  `/debug.jpg` every 30 s. HA fetches it server-side, so it works from phones outside the cluster.

Thresholds live in `water_meter.yaml`. To change them, edit the repo copy, copy it into the pod
(`kubectl exec -i -n home-assistant home-assistant-0 -- sh -c 'cat > /config/water_meter.yaml' < water_meter.yaml`)
and reload automations (Developer Tools → YAML → Automations). Changes to the `rest:` sensors need a full restart.

### Notes

- The Longhorn volume replica count was reduced from 3 to 1 during the 2026-04-11 incident. Consider scaling back to 2+ for redundancy:
  ```bash
  kubectl patch volumes.longhorn.io <vol> -n longhorn-system --type=merge -p '{"spec":{"numberOfReplicas":2}}'
  ```
