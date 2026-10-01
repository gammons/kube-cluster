# Water meter reader

Reads the house water meter (Neptune T-10 5/8", ProCoder register) from a Tapo C113
camera and serves the reading to Home Assistant. Design:
`docs/superpowers/specs/2026-10-01-water-meter-reader-design.md`.

Every 15 s the service grabs one RTSP frame, straightens it, reads the eight odometer
wheels with the AI-on-the-edge `dig-class100` model and the red sweep needle with
OpenCV, and combines them into a total in gallons. Readings that go backwards or jump
faster than 25 gal/min are rejected.

## Endpoints

In-cluster: `http://water-meter.water-meter.svc.cluster.local:8080`

| Path | Purpose |
|------|---------|
| `/reading` | JSON: `total_gal`, `flow_gpm`, `continuous_flow_minutes`, `status` (`ok`/`stale`/`error`), `last_success`, `last_error` |
| `/debug.jpg` | Latest straightened frame with digit boxes, values, dial and needle drawn on it |
| `/healthz` | Process liveness only (does not check the camera) |

From a laptop:

```bash
kubectl --context local-k3s -n water-meter port-forward deploy/water-meter 8080:8080
curl -s localhost:8080/reading
xdg-open http://localhost:8080/debug.jpg
```

## Camera

- Tapo C113 at `192.168.1.48` (keep the DHCP reservation in UniFi).
- RTSP: `rtsp://<camera account>:<password>@192.168.1.48:554/stream1`. The camera
  account is set in the Tapo app under Advanced Settings → Camera Account.
- Night vision **must be full-color** (white spotlight). In IR mode the red needle is
  pale gray, the reader finds no needle, and status goes `stale`.

## Apply

```bash
cp secret.example.yml secret.yml   # fill in the RTSP URL; never commit secret.yml
kubectl --context local-k3s apply -f ns.yml
kubectl --context local-k3s apply -f pvc.yml -f configmap.yml -f secret.yml -f deployment.yml -f service.yml
kubectl --context local-k3s -n water-meter rollout status deploy/water-meter
```

## Image

GitHub Actions (`.github/workflows/water-meter.yml`) tests and builds
`ghcr.io/gammons/water-meter` on every push to `master` that touches `water-meter/app/`.
Tags: `latest` and `sha-<short sha>`. To roll forward, change the `sha-` tag in
`deployment.yml` and apply it.

## Units

The last wheel counts 0.1 gal, and one needle turn is also 0.1 gal: each needle turn
moves the last wheel by one digit. Dial numbers are 0.01 gal, small ticks 0.001 gal.
These are `wheel_units` and `needle_units_per_rev` in `app/config.yaml`. Confirmed on
2026-10-01 with a 1-gallon bucket test: ten needle turns, last wheel advanced ten digits,
and the service measured 1.03 gal.

Each sample reads a burst of 5 frames and publishes the median when at least 3 agree
within 0.05 gal. While water runs the model occasionally misreads the spinning last wheel
on a single frame (about 2% of frames); the vote discards those.

## Recalibrating

Needed if the camera or meter moves. `app/config.yaml` is the source of truth;
`configmap.yml` is generated from it.

```bash
cd app
uv venv --python 3.12 .venv && uv pip install --python .venv -r requirements-dev.txt
ffmpeg -rtsp_transport tcp -i "$RTSP_URL" -frames:v 1 /tmp/frame.jpg
.venv/bin/python -m water_meter.calibrate /tmp/frame.jpg config.yaml /tmp/cal
# view /tmp/cal/overlay.png and /tmp/cal/digit_*.png; edit config.yaml; repeat
```

Done when the digit row is level, each digit box is centred on its glyph, the small
circle sits on the red hub, the large circle just inside the tick ring, and the zero
tick points at the dial's `0`. The digit model is sensitive to 2 px offsets.

If you recalibrate, replace `app/tests/fixtures/color_177255.jpg` with a new frame and
update the expected readings in the tests. Then regenerate the ConfigMap and restart:

```bash
kubectl create configmap water-meter-config -n water-meter \
  --from-file=config.yaml=app/config.yaml --dry-run=client -o yaml > configmap.yml
kubectl --context local-k3s apply -f configmap.yml
kubectl --context local-k3s -n water-meter rollout restart deploy/water-meter
```

## Troubleshooting

| `last_error` | Likely cause |
|--------------|--------------|
| `capture: ...` | Camera offline, IP changed, or wrong RTSP credentials |
| `needle not found` | Spotlight off / IR mode, or something blocking the dial |
| `low digit confidence: wheel N` | Glare, blocked view, or digit boxes drifted |
| `inconsistent wheels and needle` | Misread last wheel, or wrong unit settings |
| `rejected: ...` | Implausible jump. After 10 minutes of consistent new values the service accepts them |

State (last total, leak streak start) is kept in `/state/state.json` on the
`water-meter-state` PVC. Deleting it makes the next reading start fresh.
