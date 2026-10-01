# Water Meter Reader Design

## Date

2026-10-01

## Goal

Read the house water meter from a fixed camera, publish the reading through a small in-cluster HTTP service, and have Home Assistant track usage and alert on leaks.

Motivation: a large water bill was traced to a leaking hose and a leaky toilet. Both are fixed; the goal is to catch the next leak in hours, not at the next bill.

## Success criteria

- Home Assistant shows a monotonically increasing water total usable by the Energy dashboard's water section.
- A slow leak (running toilet, ~0.02-0.2 gal/min) triggers a phone alert within about 2 hours.
- A large sustained flow (hose left on, burst pipe) triggers a phone alert within about 20 minutes.
- Loss of a readable image (camera offline, spotlight off, view blocked) triggers a phone alert within about 30 minutes.
- Misreads never move the published total backwards or produce impossible jumps.

## Context

- Meter: Neptune T-10 5/8" with ProCoder register. Eight odometer wheels (first four on a white background, last four on a dark one) and a red sweep needle on a 0-9 dial labelled `x.01`.
- Camera: Tapo C113 at `192.168.1.48`, permanently mounted, will never move.
  - RTSP: `rtsp://<user>:<pass>@192.168.1.48:554/stream1`, H.264, 2304x1296. Credentials are a Tapo "camera account".
  - Night vision mode is set to **full color** (white spotlight). This is required: under IR the red needle renders pale gray on a white face and cannot be segmented reliably.
  - Burned-in timestamp (top left) and `tapo` logo (bottom left) do not overlap the meter.
- Measured from a real frame:
  - Digit pitch about 30 px, glyphs about 20x30 px. The digit row is rotated about 10 degrees.
  - Sweep dial about 190 px in diameter. The needle hub is also red.
  - No glare on the digit window or dial in color mode.
- Cluster context: `local-k3s`. Home Assistant runs in the `home-assistant` namespace and already reaches LAN devices on `192.168.1.0/24` (Envisalink at `192.168.1.32`).
- Repository: `gammons/kube-cluster` on GitHub. No existing GitHub Actions workflows.

## Approach

Local computer vision against fixed, calibrated regions of the frame:

- Odometer wheels: the pretrained digit classifier from the AI-on-the-edge-device project (`dig-class100` family), which outputs a value in 0.0-9.9 so a wheel caught mid-roll reads as a fraction instead of a guess.
- Sweep needle: OpenCV red-pixel segmentation and angle from the calibrated dial center.

Rejected: sending frames to a vision LLM via CLIProxyAPI (too slow and costly at the sampling rate, imprecise on needle angle) and Tesseract OCR (poor on rolling odometer wheels).

## Design

### Layout

New directory `water-meter/` in this repository:

| Path | Purpose |
|---|---|
| `water-meter/ns.yml` | Namespace `water-meter` |
| `water-meter/pvc.yml` | State PVC |
| `water-meter/configmap.yml` | Calibration and tuning |
| `water-meter/secret.example.yml` | Template for the RTSP credentials Secret |
| `water-meter/deployment.yml` | Single-replica Deployment |
| `water-meter/service.yml` | ClusterIP Service |
| `water-meter/README.md` | Apply steps, calibration procedure, troubleshooting |
| `water-meter/app/` | Python source, tests, `Dockerfile` |
| `.github/workflows/water-meter.yml` | Test, build and push the image |

`water-meter/secret.yml` (the real Secret) is added to `.gitignore`.

### Image and CI

- Image: `ghcr.io/gammons/water-meter`, package visibility **public** (no secrets in the image, so no pull secret is needed in the cluster).
- Workflow triggers on pushes to `main` touching `water-meter/app/**` (and the workflow file). It runs `pytest`, then builds and pushes `linux/amd64` tags `latest` and `sha-<short-sha>`.
- `deployment.yml` pins the `sha-<short-sha>` tag; rolling forward is an explicit manifest edit.
- Base image: `python:3.12-slim`. Dependencies: FastAPI, uvicorn, `opencv-python-headless`, numpy, and the LiteRT runtime (`ai-edge-litert`) for the `.tflite` digit model.
- The digit model file is downloaded at image build time from a pinned upstream release and its license is recorded in `water-meter/app/`. The exact model file and input shape are confirmed during implementation from the model's metadata.

### Service internals

Python package `water_meter` with one module per job:

| Module | Responsibility |
|---|---|
| `capture` | Every 15 s: open the RTSP stream over TCP, discard the first few frames, keep one, close the stream. Reconnecting per sample is more robust than a long-lived Tapo connection. |
| `regions` | Load calibration from the ConfigMap. Deskew the frame by the calibrated rotation, then crop each digit box and the dial. |
| `digits` | Run the classifier on each of the 8 digit crops; return a value in [0.0, 10.0) per wheel. |
| `needle` | Within the dial crop, take an annulus between the hub and the rim, segment red pixels in HSV, and compute the needle angle from the dial center. Return a fraction of one revolution in [0.0, 1.0), or "not found". |
| `combine` | Build one total from wheels and needle (below), then apply the plausibility checks. |
| `flow` | Keep a rolling history of accepted readings; compute `flow_gpm` and `continuous_flow_minutes`. |
| `state` | Persist the last accepted reading and the current flow-streak start to a JSON file on the PVC; load on startup. |
| `debug` | Render the annotated debug image. |
| `api` | HTTP endpoints. |

### Units

Unit values live in the ConfigMap so they can be corrected without a rebuild:

- `wheel_units`: value of the last odometer wheel, default `0.1` gallon. The wheels advanced about 1,700 counts in two days; at 0.1 gal that is about 85 gal/day, which matches the household. At 1 gal it would be 850 gal/day, which is implausible. The current reading is therefore about 17,725.5 gallons.
- `needle_units_per_rev`: value of one needle revolution, default `1` gallon (dial numbers 0-9 are 0.1 gal, minor ticks 0.01 gal).

Defaults are confirmed during calibration with a bucket test (run a known volume, watch needle and wheels) and by the wheel/needle consistency check below.

### Combining a reading

The needle is authoritative within one revolution; the wheels only decide which revolution the meter is on.

1. Resolve the wheels right to left into one value `W` in gallons. Each wheel's fractional reading decides whether the wheel to its left has rolled (the AI-on-the-edge post-processing rule); the last wheel keeps its fraction.
2. With `R = needle_units_per_rev` and needle fraction `f`, choose the integer `k` that puts `R * (k + f)` closest to `W`. Then `total = R * (k + f)`.
3. If `|total - W|` exceeds `consistency_tolerance` (default `0.3 * R`), the wheels and the needle disagree and the sample is discarded. This also catches wrong unit settings during calibration.
4. Flow at any rate is derived from changes in `total`, so the needle spinning several revolutions between samples at high flow does not cause aliasing.

If the needle is not found, or any wheel's classifier confidence is below a configured threshold, the sample is discarded.

### Plausibility checks

Against the last accepted reading `prev` at time `t_prev`:

- Small backwards moves within `jitter_gal` (default 0.02) are clamped to `prev` (needle noise).
- Larger backwards moves are rejected.
- Increases greater than `max_gpm * elapsed_minutes + jitter_gal` are rejected (`max_gpm` default 25).
- Recovery: if every sample for `recovery_minutes` (default 10) has been rejected and they agree with each other within 1 gallon, accept the new value, log a warning, and continue from it. This prevents one bad stored value from blocking readings forever.

### Flow and leak signals

- A 15-minute window is "still" when the spread of `total` across all accepted samples in it is below `still_threshold_gal` (default 0.01, one needle tick). Stillness is judged over the whole window, not between consecutive samples, because a slow leak moves the needle less than one tick per 15 s sample (0.02 gal/min is 0.005 gal per sample, but 0.3 gal per window).
- `flow_gpm`: change in `total` over the last 5 minutes of accepted samples, divided by elapsed minutes.
- `continuous_flow_minutes`: minutes since the end of the most recent still window. A normal house has such a window daily; a leak never does. The streak start is persisted so a pod restart does not reset it.

### API

Port 8080.

`GET /reading`:

```json
{
  "total_gal": 17725.556,
  "flow_gpm": 0.0,
  "continuous_flow_minutes": 0,
  "status": "ok",
  "last_success": "2026-10-01T20:59:46Z",
  "last_error": null
}
```

- `status`: `ok`; `stale` when there has been no accepted reading for 5 minutes (last good values are still returned); `error` when there has never been an accepted reading since startup and no persisted state exists.
- `last_error`: short description of the most recent failure (RTSP connect, needle not found, low confidence, rejected by plausibility), or `null`.

`GET /debug.jpg`: the latest deskewed frame with digit boxes and per-digit values, dial circle, hub/rim annulus, detected needle line, and the combined total drawn on it.

`GET /healthz`: process liveness only. It does not depend on the camera, so a camera outage does not restart the pod or hide the `stale` status.

### Calibration

Calibration is stored in `configmap.yml`:

- `rotation_deg` and `meter_crop` (box in the original frame) used to deskew.
- Eight digit boxes, in deskewed-crop coordinates.
- Dial `center`, `hub_radius`, and `rim_radius`, plus `zero_angle_deg` (the angle of the 0 mark), in deskewed-crop coordinates.
- Unit values and tuning thresholds from the sections above.

Procedure: capture a frame, determine the values, apply the ConfigMap, restart the pod, verify with `/debug.jpg`. The README documents this. The initial calibration is done during implementation from live frames.

### Kubernetes resources

- Deployment: 1 replica, strategy `Recreate`, no node pinning. Requests 100m CPU / 192Mi, limits 500m CPU / 384Mi.
- Liveness and readiness probes: `GET /healthz`.
- Environment: `RTSP_URL` from Secret `water-meter-rtsp`, key `url`.
- Volumes: ConfigMap mounted at `/config`, PVC mounted at `/state`.
- PVC `water-meter-state`: storage class `nfs`, 1Gi, `ReadWriteOnce`.
- Service `water-meter`: ClusterIP, port 8080. Home Assistant uses `http://water-meter.water-meter.svc.cluster.local:8080`. No LAN or Tailscale exposure in this iteration.

### Home Assistant

A package file `water_meter.yaml` on the Home Assistant PVC, included from `configuration.yaml` via `homeassistant: packages:`. A copy is kept at `home-assistant/water_meter.yaml` and documented in `home-assistant/README.md`, following the Envisalink pattern.

The package contains:

- One `rest:` resource polling `/reading` every 30 s, defining:
  - `sensor.water_meter_total`: gallons, `device_class: water`, `state_class: total_increasing`. Added to the Energy dashboard's water section.
  - `sensor.water_flow`: gal/min, `state_class: measurement`.
  - `sensor.water_continuous_flow`: minutes.
  - `sensor.water_meter_status`: `ok` / `stale` / `error`.
- Three automations sending a mobile app push notification. The notify target is the user's existing `notify.mobile_app_*` service, identified during implementation.
  1. Slow leak: `sensor.water_continuous_flow` above 120 for any duration.
  2. Large flow: `sensor.water_flow` above 1.0 for 20 minutes.
  3. Meter unreadable: `sensor.water_meter_status` not `ok` for 30 minutes.

## Error handling summary

| Failure | Behaviour |
|---|---|
| Camera unreachable / RTSP error | Sample skipped, `last_error` set; `stale` after 5 minutes; HA alert after 30 minutes. |
| Needle not found or low digit confidence | Sample skipped, `last_error` set. |
| Implausible reading | Sample rejected; recovery rule after 10 minutes of consistent rejections. |
| Pod restart | Last accepted reading and flow-streak start restored from the PVC. |
| Spotlight off (IR mode) | Needle not found, so status goes `stale` and the unreadable alert fires. |

## Testing

- Unit tests for `combine` and `flow` as pure functions: wheel roll resolution, needle wrap-around, jitter clamp, backwards and over-max rejection, recovery, still-window and streak logic, persistence round trip.
- Image tests on real frames captured from this camera, stored as fixtures in `water-meter/app/tests/fixtures/` with their known readings: needle fraction within 0.01 revolution and wheel values correct.
- CI runs the full test suite before building the image.
- Acceptance after deployment: `/reading` returns `ok`, `/debug.jpg` overlays align, the total tracks a known draw (bucket test), and each HA alert is exercised once (leaving a tap dripping, running a tap, unplugging the camera).

## Out of scope

- LAN or Tailscale exposure of the service.
- Multiple meters or cameras.
- Automatic recalibration if the camera moves.
- Prometheus metrics.
