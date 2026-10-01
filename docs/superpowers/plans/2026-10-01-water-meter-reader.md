# Water Meter Reader Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A small in-cluster service reads the water meter from the Tapo C113 camera, serves the reading over HTTP, and Home Assistant tracks usage and alerts on leaks.

**Architecture:** A single Python process samples one RTSP frame every 15 s, deskews and crops calibrated regions, reads the eight odometer wheels with the AI-on-the-edge `dig-class100` TFLite model and the sweep needle with OpenCV red segmentation, combines them into a plausibility-checked total, and tracks flow and leak streaks. FastAPI exposes `/reading`, `/debug.jpg`, `/healthz`. Home Assistant polls `/reading` with a `rest:` package and runs three alert automations.

**Tech Stack:** Python 3.12, FastAPI + uvicorn, opencv-python-headless, numpy, ai-edge-litert, PyYAML, pytest; Docker; GitHub Actions + GHCR; k3s manifests; Home Assistant YAML package.

**Spec:** `docs/superpowers/specs/2026-10-01-water-meter-reader-design.md`

## Global Constraints

- Kubernetes context: `local-k3s`. Namespace: `water-meter`. Git branch: `master`.
- Image: `ghcr.io/gammons/water-meter`, `linux/amd64`, tags `latest` and `sha-<short-sha>`; Deployment pins the `sha-` tag. Package visibility public.
- Camera: `rtsp://camera:two3four@192.168.1.48:554/stream1`, RTSP over TCP, 2304x1296. Full-color night mode is required.
- Sample interval 15 s. HA polls every 30 s.
- Units: `wheel_units` 0.1 gal, `needle_units_per_rev` 1.0 gal. Config load must reject any config where `needle_units_per_rev != 10 * wheel_units`.
- Tuning defaults: `consistency_tolerance` 0.3 (revolutions), `jitter_gal` 0.02, `max_gpm` 25, `recovery_minutes` 10, `recovery_agree_gal` 1.0, `still_threshold_gal` 0.01, `still_window_minutes` 15, `flow_window_minutes` 5, `stale_minutes` 5, `min_digit_confidence` 0.6.
- Digit model: `dig-class100-0182-s2_q.tflite` from `jomjol/AI-on-the-edge-device` commit `384079b5d459f4730dc64d36ba4071bb02349d93`, path `sd-card/config/`. Input `[1, 32, 20, 3]` float32 RGB, raw 0-255 (not normalized). Output `[1, 100]` logits; class `k` means wheel value `k / 10`. Committed to the repo with the upstream `Licence.md` (private non-commercial use; must keep the license and source reference).
- Service port 8080. Paths from env: `RTSP_URL` (required), `CONFIG_PATH` default `/config/config.yaml`, `STATE_PATH` default `/state/state.json`, `MODEL_PATH` default `/app/models/dig-class100-0182-s2_q.tflite`.
- Resources: requests 100m / 192Mi, limits 500m / 384Mi. PVC `water-meter-state`, class `nfs`, 1Gi, RWO. Service ClusterIP `water-meter:8080`.
- HA alerts go to `notify.mobile_app_pixel_10_pro` only. Thresholds: continuous flow > 120 min; flow > 1.0 gal/min for 20 min; status not `ok` for 30 min.
- All timestamps are timezone-aware UTC; API renders them ISO 8601 with `Z`.
- Never commit `water-meter/secret.yml`.

## Review Focus

1. **Spotlight off (camera falls back to IR)** — the needle is gray, so no reading must be produced at all (status goes `stale`), never a guessed total. Pinned by `test_ir_frame_has_no_needle` in Task 5.
2. **Needle pointing near 0 (wrap-around)** — a naive mean of angles near 359° and 1° gives 180°; readings must stay near 0. Pinned by `test_needle_wraparound_near_zero` in Task 5.
3. **Camera hangs or drops mid-sample** — the sampling loop must record the error and keep sampling, and `/reading` must still return the last good total. Pinned by `test_capture_failure_keeps_last_total` in Task 7.
4. **State file missing, empty or half-written** (NFS hiccup, crash during write) — the service starts fresh instead of crash-looping. Pinned by `test_load_state_corrupt_returns_none` in Task 3.
5. **Pod restarted during a leak** — the continuous-flow streak must continue from its persisted start, not reset to zero and delay the alert by 2 hours. Pinned by `test_streak_survives_restart` in Task 3.

---

## File Structure

```
water-meter/
  ns.yml  pvc.yml  configmap.yml  secret.example.yml  deployment.yml  service.yml  README.md
  app/
    Dockerfile  pyproject.toml  requirements.txt  requirements-dev.txt
    config.yaml                      # calibration + tuning; source of truth, copied into configmap.yml
    models/dig-class100-0182-s2_q.tflite  models/Licence.md  models/SOURCE.md
    water_meter/
      __init__.py  __main__.py
      config.py      # Calibration/Tuning/Config dataclasses, load_config
      combine.py     # resolve_reading, Plausibility
      flow.py        # FlowTracker
      state.py       # PersistedState, load_state, save_state
      regions.py     # deskew, digit_crops
      needle.py      # read_needle
      digits.py      # DigitReader, DigitResult
      debug.py       # render
      calibrate.py   # CLI: python -m water_meter.calibrate
      capture.py     # grab_frame, CaptureError
      service.py     # MeterService
      api.py         # create_app
    tests/
      fixtures/color_177255.jpg  fixtures/ir.jpg
      test_config.py test_combine.py test_plausibility.py test_flow.py test_state.py
      test_needle.py test_digits.py test_service.py test_api.py
.github/workflows/water-meter.yml
home-assistant/water_meter.yaml
.gitignore                            # add water-meter/secret.yml, water-meter/app/.venv
```

Run all Python commands from `water-meter/app` inside `.venv`.

---

### Task 1: Project skeleton and config loading

**Files:**
- Create: `water-meter/app/pyproject.toml`, `requirements.txt`, `requirements-dev.txt`, `water_meter/__init__.py`, `water_meter/config.py`, `config.yaml`, `tests/test_config.py`
- Modify: `.gitignore`

**Interfaces:**
- Produces:
  - `Calibration` (frozen dataclass): `rotation_deg: float`, `meter_crop: tuple[int, int, int, int]` (x, y, w, h in the original frame), `digit_boxes: list[tuple[int, int, int, int]]` (8 boxes, x, y, w, h in deskewed-crop coords, left to right), `dial_center: tuple[float, float]`, `hub_radius: float`, `rim_radius: float`, `zero_angle_deg: float` (clockwise from straight up, deskewed coords).
  - `Tuning` (frozen dataclass) with the Global Constraints fields and defaults, plus `sample_interval_s: float = 15`.
  - `Config` (frozen dataclass): `calibration: Calibration`, `tuning: Tuning`.
  - `load_config(path: Path) -> Config`; raises `ConfigError(ValueError)`.
  - `config.yaml` top-level keys `calibration:` and `tuning:` mirroring the dataclass field names. Omitted tuning keys take defaults.

- [ ] **Step 1: Set up the environment**

`requirements.txt`: `fastapi`, `uvicorn`, `opencv-python-headless`, `numpy`, `ai-edge-litert`, `pyyaml` (pin current versions). `requirements-dev.txt`: `-r requirements.txt`, `pytest`, `httpx`. `pyproject.toml` sets `[tool.pytest.ini_options] testpaths = ["tests"]`. Add `water-meter/secret.yml` and `water-meter/app/.venv/` to the root `.gitignore`.

Run: `uv venv --python 3.12 .venv && uv pip install --python .venv -r requirements-dev.txt`
Expected: installs without errors.

- [ ] **Step 2: Write the failing tests**

```python
def test_load_valid_config(tmp_path): ...      # loads a config with 8 boxes; tuning.wheel_units == 0.1, tuning.max_gpm == 25
def test_missing_tuning_uses_defaults(tmp_path): ...  # tuning: {} -> Tuning() defaults
def test_wrong_box_count_rejected(tmp_path): ...      # 7 boxes -> ConfigError
def test_unit_mismatch_rejected(tmp_path): ...        # wheel_units 1.0, needle_units_per_rev 1.0 -> ConfigError
def test_unknown_key_rejected(tmp_path): ...          # tuning: {max_gmp: 30} -> ConfigError (typo protection)
def test_repo_config_loads(): ...                     # load_config(Path("config.yaml")) succeeds
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_config.py -v`
Expected: FAIL, `ModuleNotFoundError: water_meter.config`.

- [ ] **Step 4: Implement `config.py` and a placeholder-free `config.yaml`**

`config.yaml` holds provisional calibration so it loads: `rotation_deg: 12.0`, `meter_crop: [780, 550, 500, 500]`, eight 22x32 boxes on a 32 px pitch, `dial_center: [290, 330]`, `hub_radius: 12`, `rim_radius: 90`, `zero_angle_deg: 0`. Task 4 replaces these with measured values.

- [ ] **Step 5: Run tests to verify they pass**

Run: `.venv/bin/pytest -v`
Expected: 6 passed.

- [ ] **Step 6: Commit**

```bash
git add .gitignore water-meter/app
git commit -m "add water meter app skeleton and config loading"
```

---

### Task 2: Reading combination and plausibility

**Files:**
- Create: `water-meter/app/water_meter/combine.py`, `tests/test_combine.py`, `tests/test_plausibility.py`

**Interfaces:**
- Consumes: `Tuning` (Task 1).
- Produces:
  - `class InconsistentReading(ValueError)`.
  - `resolve_reading(wheels: list[float], needle: float, tuning: Tuning) -> float` — `wheels` are 8 raw model values in [0, 10), left to right; `needle` is a fraction in [0, 1). Returns total gallons.
  - `class Plausibility(tuning: Tuning, last_total: float | None = None, last_ts: datetime | None = None)` with `check(total: float, ts: datetime) -> float | None` (accepted value, possibly clamped, or `None` if rejected), properties `last_total`, `last_ts`, and `last_rejection: str | None` (reason for the most recent rejection).

`resolve_reading` algorithm (the spec's rule, made concrete for this meter: the last wheel turns continuously with the needle, the wheels left of it advance only while their right neighbour passes 9 to 0):

```
cd(a, b) = circular distance on a 0..10 dial
v = needle * 10                                  # value of the last (0.1 gal) wheel
if cd(wheels[7], v) > tuning.consistency_tolerance * 10: raise InconsistentReading
right = v; digits = []
for w in wheels[6::-1]:                          # wheels 6..0, right to left
    e = max(0.0, right - 9.0)                    # expected fractional advance of this wheel
    n = the integer 0..9 minimising cd(w, n + e)
    digits.insert(0, n); right = n + e
total = (int("".join(digits)) + needle) * tuning.needle_units_per_rev
```

- [ ] **Step 1: Write the failing tests**

```python
# test_combine.py, tuning = Tuning()
def test_plain_reading():          # [0,1,7,7,2,5,5.0,4.8], 0.48 -> approx 177255.48
def test_misread_neighbour_rounds(): # [0,1,7,7,2,5,4.8,5.4], 0.48 -> approx 177255.48 (frame from the camera)
def test_just_rolled():            # [0,1,7,7,2,5,5.8,0.3], 0.02 -> approx 177256.02
def test_mid_roll_not_yet():       # [0,1,7,7,2,5,5.4,9.5], 0.95 -> approx 177255.95
def test_cascade_before():         # [0,1,7,7.5,9.5,9.6,9.5,9.6], 0.96 -> approx 177999.96
def test_cascade_after():          # [0,1,7,8.0,0.0,0.0,0.1,0.2], 0.01 -> approx 178000.01
def test_last_wheel_wraps_ok():    # [...,9.8], needle 0.01 -> no exception
def test_inconsistent_raises():    # [0,1,7,7,2,5,5,8.0], 0.48 -> InconsistentReading

# test_plausibility.py, t0 = 2026-10-01T20:00Z
def test_first_reading_accepted():        # Plausibility(Tuning()).check(100.0, t0) == 100.0
def test_small_backwards_clamped():       # last 100.0; check(99.99, t0+15s) == 100.0
def test_large_backwards_rejected():      # last 100.0; check(99.5, t0+15s) is None; last_total stays 100.0
def test_over_max_rejected():             # last 100.0; check(100.0 + 25*0.25 + 0.03, t0+15s) is None
def test_at_max_accepted():               # last 100.0; check(100.0 + 25*0.25, t0+15s) == that value
def test_recovery_after_consistent_rejections():  # last 100.0; 41 samples of 500.0..500.5 every 15s -> None until 10 min elapsed, then accepted
def test_no_recovery_when_rejections_disagree():  # alternating 500.0 / 900.0 for 11 min -> all None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_combine.py tests/test_plausibility.py -v`
Expected: FAIL, `ImportError`.

- [ ] **Step 3: Implement `combine.py`**

Recovery: keep the rejected `(total, ts)` samples since the last acceptance; once the first is at least `recovery_minutes` old and their spread is at most `recovery_agree_gal`, accept the newest, log a warning, clear the list. Any acceptance clears the list.

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest -v`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add water-meter/app
git commit -m "add water meter reading combination and plausibility checks"
```

---

### Task 3: Flow tracking and persisted state

**Files:**
- Create: `water-meter/app/water_meter/flow.py`, `water_meter/state.py`, `tests/test_flow.py`, `tests/test_state.py`

**Interfaces:**
- Consumes: `Tuning`.
- Produces:
  - `class FlowTracker(tuning: Tuning, streak_start: datetime | None = None)` with `add(total: float, ts: datetime) -> None`, `flow_gpm(now: datetime) -> float`, `continuous_flow_minutes(now: datetime) -> int`, property `streak_start: datetime | None`.
  - `@dataclass PersistedState(last_total: float, last_ts: datetime, streak_start: datetime | None)`.
  - `load_state(path: Path) -> PersistedState | None` (missing, empty or unparseable file returns `None` and logs a warning).
  - `save_state(path: Path, state: PersistedState) -> None` (write to a temp file in the same directory, then `os.replace`).

Rules:
- On `add`, if no streak start is set, set it to `ts`.
- After `add`, the trailing window is "still" when the history contains a sample at or before `ts - still_window_minutes` and the spread (max - min) of totals from that sample to `ts` is below `still_threshold_gal`. When still, `streak_start = ts`.
- `continuous_flow_minutes(now)` = whole minutes between `streak_start` and `now`; 0 if unset.
- `flow_gpm(now)`: totals within the last `flow_window_minutes`; change divided by elapsed minutes between the first and last of them; 0.0 when fewer than 2 samples or they span under 1 minute.
- Keep at most `still_window_minutes + 1` minutes of history.

- [ ] **Step 1: Write the failing tests**

```python
# samples every 15 s from t0 = 2026-10-01T20:00Z
def test_no_flow_resets_streak():        # constant 100.0 for 16 min -> continuous_flow_minutes == 0
def test_slow_leak_accumulates():        # +0.02 gal/min for 130 min -> continuous_flow_minutes >= 120
def test_sub_tick_noise_is_still():      # totals alternate 100.000/100.005 for 16 min -> 0
def test_leak_then_quiet():              # leak 60 min, then constant 16 min -> 0
def test_flow_gpm():                     # +1.5 gal/min for 6 min -> flow_gpm approx 1.5
def test_flow_gpm_insufficient_data():   # one sample -> 0.0
def test_streak_survives_restart():      # leak 60 min; new FlowTracker(streak_start=old.streak_start); add leak samples for 61 more min -> >= 120

# test_state.py
def test_round_trip(tmp_path):                 # save then load equals original (tz-aware UTC, streak_start None and set)
def test_load_missing_returns_none(tmp_path):
def test_load_state_corrupt_returns_none(tmp_path):  # empty file and truncated JSON -> None
def test_save_is_atomic(tmp_path):             # no stray temp files remain after save
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_flow.py tests/test_state.py -v`
Expected: FAIL, `ImportError`.

- [ ] **Step 3: Implement `flow.py` and `state.py`**

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest -v`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add water-meter/app
git commit -m "add water flow tracking and persisted state"
```

---

### Task 4: Regions, debug overlay, calibration tool and real calibration

**Files:**
- Create: `water-meter/app/water_meter/regions.py`, `water_meter/debug.py`, `water_meter/calibrate.py`, `tests/fixtures/color_177255.jpg`, `tests/fixtures/ir.jpg`
- Modify: `water-meter/app/config.yaml`

**Interfaces:**
- Consumes: `Calibration`, `load_config`.
- Produces:
  - `deskew(frame: np.ndarray, cal: Calibration) -> np.ndarray` — rotate the full frame by `rotation_deg` (OpenCV convention, counter-clockwise positive) about the centre of `meter_crop`, then return the `meter_crop` region. All later coordinates are in this image.
  - `digit_crops(meter: np.ndarray, cal: Calibration) -> list[np.ndarray]` — 8 BGR crops.
  - `render(meter: np.ndarray, cal: Calibration, digit_values: list[float] | None = None, needle: float | None = None, total: float | None = None) -> np.ndarray` — draws digit boxes (with values above them when given), hub and rim circles, a tick at `zero_angle_deg`, the needle line when given, and the total text.
  - CLI `python -m water_meter.calibrate <frame.jpg> <config.yaml> <out_dir>` writes `out_dir/overlay.png` (render at 3x scale) and `out_dir/digit_<i>.png` (each crop at 4x).

- [ ] **Step 1: Add fixtures**

Copy `/tmp/wm.82zZ/frame4.jpg` to `tests/fixtures/color_177255.jpg` (full-color frame; wheels `0177255`, last wheel mid-roll, needle about 0.48) and `/tmp/wm.82zZ/frame3.jpg` to `tests/fixtures/ir.jpg` (IR frame). If `/tmp` was cleared, recapture with `ffmpeg -rtsp_transport tcp -i "rtsp://camera:two3four@192.168.1.48:554/stream1" -frames:v 1 out.jpg` and ask the user to switch night vision to IR for the IR fixture, then back to full color. Record the readings visible in any recaptured frame instead of the values above.

- [ ] **Step 2: Implement `regions.py`, `debug.py`, `calibrate.py`**

- [ ] **Step 3: Calibrate against the colour fixture**

Iterate: edit `config.yaml`, run `.venv/bin/python -m water_meter.calibrate tests/fixtures/color_177255.jpg config.yaml /tmp/opencode-cal` and view `overlay.png` and the digit crops. Done when:
- The digit row is horizontal in the overlay (`rotation_deg` near 12).
- Each digit crop is centred on its glyph with 1-3 px margin on each side and the glyph fills the height.
- The hub circle sits on the red hub; the rim circle sits just inside the tick ring; the zero tick points at the dial's `0`.

Exploration found the digit row centre near (1042, 748) in the original frame and the hub near (1068, 895); the model is sensitive to 2 px shifts, so centre carefully.

- [ ] **Step 4: Verify config still loads**

Run: `.venv/bin/pytest tests/test_config.py -v`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add water-meter/app
git commit -m "add water meter region cropping, calibration tool and initial calibration"
```

---

### Task 5: Needle reader

**Files:**
- Create: `water-meter/app/water_meter/needle.py`, `tests/test_needle.py`

**Interfaces:**
- Consumes: `Calibration`, `deskew` (Task 4).
- Produces: `read_needle(meter: np.ndarray, cal: Calibration, min_pixels: int = 20) -> float | None` — fraction of a revolution in [0, 1) from `zero_angle_deg`, clockwise; `None` when fewer than `min_pixels` red pixels lie in the annulus.

Approach: HSV mask with hue < 10 or > 170, saturation > 60, value > 60; keep pixels whose distance from `dial_center` is between `hub_radius * 1.3` and `rim_radius`; angle per pixel `atan2(dx, -dy)` (clockwise from up); combine with a circular mean (mean of unit vectors); fraction = `((angle - zero_angle_deg) mod 360) / 360`.

- [ ] **Step 1: Write the failing tests**

```python
def synthetic_dial(angle_deg): ...   # white 200x200 image, red line from centre (100,100) to radius 80 at angle_deg clockwise from up, matching Calibration(dial_center=(100,100), hub_radius=8, rim_radius=90, zero_angle_deg=0, ...)

@pytest.mark.parametrize("deg", [0, 45, 90, 180, 270, 300])
def test_synthetic_angles(deg):      # read_needle == approx(deg/360, abs=0.005)
def test_needle_wraparound_near_zero():  # line at 359.5 deg -> result < 0.003 or > 0.997
def test_no_red_returns_none():          # plain white image -> None
def test_hub_only_returns_none():        # red disc of radius 8 at centre only -> None
def test_real_frame():                   # deskew(color fixture) with repo config -> approx 0.485, abs=0.03
def test_ir_frame_has_no_needle():       # deskew(IR fixture) -> None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_needle.py -v`
Expected: FAIL, `ImportError`.

- [ ] **Step 3: Implement `read_needle`**

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest -v`
Expected: all pass. If `test_real_frame` fails, view the calibrate overlay before changing the algorithm; a wrong `dial_center` or `zero_angle_deg` is the likely cause.

- [ ] **Step 5: Commit**

```bash
git add water-meter/app
git commit -m "add water meter needle reader"
```

---

### Task 6: Digit reader

**Files:**
- Create: `water-meter/app/models/dig-class100-0182-s2_q.tflite`, `models/Licence.md`, `models/SOURCE.md`, `water_meter/digits.py`, `tests/test_digits.py`

**Interfaces:**
- Consumes: `digit_crops`, `deskew`.
- Produces:
  - `@dataclass(frozen=True) DigitResult(value: float, confidence: float)`.
  - `class DigitReader(model_path: Path)` with `read(crop: np.ndarray) -> DigitResult`.

Preprocessing and decoding: resize the BGR crop to width 20, height 32 (`cv2.INTER_LINEAR`), convert to RGB, `float32` without scaling, add batch dim. Softmax the 100 logits. `value = argmax / 10`; `confidence` = sum of probabilities at `argmax - 1`, `argmax`, `argmax + 1` (indices modulo 100).

- [ ] **Step 1: Vendor the model**

Download `dig-class100-0182-s2_q.tflite` and `Licence.md` from `https://raw.githubusercontent.com/jomjol/AI-on-the-edge-device/384079b5d459f4730dc64d36ba4071bb02349d93/...` into `models/`. `SOURCE.md` states the upstream repo, commit, file path, and that the files are unmodified and used for private non-commercial purposes.

- [ ] **Step 2: Write the failing tests**

```python
def test_model_shapes():          # interpreter input shape [1,32,20,3], output [1,100]
def test_real_frame_digits():     # colour fixture: first 7 values within circular distance 0.3 of [0,1,7,7,2,5,5]; each of those confidence >= 0.6
def test_real_frame_resolves():   # resolve_reading(values, read_needle(...), Tuning()) == approx(177255.48, abs=0.04)
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_digits.py -v`
Expected: FAIL, `ImportError`.

- [ ] **Step 4: Implement `digits.py`**

- [ ] **Step 5: Run tests to verify they pass**

Run: `.venv/bin/pytest -v`
Expected: all pass. A failing digit usually means its box is off by a few pixels; fix `config.yaml` with the calibrate tool, not the tolerance.

- [ ] **Step 6: Commit**

```bash
git add water-meter/app
git commit -m "add water meter digit reader with vendored dig-class100 model"
```

---

### Task 7: Capture, service loop and HTTP API

**Files:**
- Create: `water-meter/app/water_meter/capture.py`, `water_meter/service.py`, `water_meter/api.py`, `water_meter/__main__.py`, `tests/test_service.py`, `tests/test_api.py`

**Interfaces:**
- Consumes: everything above.
- Produces:
  - `class CaptureError(RuntimeError)`; `grab_frame(rtsp_url: str, discard: int = 5, timeout_s: float = 10.0) -> np.ndarray`. Uses `cv2.VideoCapture(url, cv2.CAP_FFMPEG)` with `OPENCV_FFMPEG_CAPTURE_OPTIONS="rtsp_transport;tcp"` set before opening, open and read timeouts from `timeout_s`, reads `discard` frames then returns the next, always releases.
  - `class MeterService(config: Config, reader: DigitReader, grab: Callable[[], np.ndarray], state_path: Path, now: Callable[[], datetime])`:
    - `sample() -> None`: grab, deskew, needle, digits, confidence check, `resolve_reading`, `Plausibility.check`, `FlowTracker.add`, `save_state`, store the debug overlay. Every failure is caught, logged, and stored as `last_error` (`"capture: ..."`, `"needle not found"`, `"low digit confidence: wheel <i>"`, `"inconsistent wheels and needle"`, `"rejected: <reason>"`); nothing raises out of `sample()`.
    - `snapshot() -> dict` with the spec's `/reading` keys. `total_gal` rounded to 3 decimals, `flow_gpm` to 3. `status`: `ok` if `last_success` is within `stale_minutes`, `stale` if older, `error` if there has never been a good reading (none in memory or state). On startup, state from `state_path` seeds `Plausibility`, `FlowTracker.streak_start` and `last_success`.
    - `debug_jpeg() -> bytes | None`.
  - `create_app(service: MeterService, run_loop: bool = True) -> FastAPI`; when `run_loop`, a background thread calls `sample()` every `sample_interval_s` from startup.
  - `python -m water_meter` builds everything from env (Global Constraints) and runs uvicorn on `0.0.0.0:8080`.

- [ ] **Step 1: Write the failing tests**

Tests build `MeterService` with a fake `grab` returning the colour fixture (or raising `CaptureError`), the real `DigitReader`, the repo config, `tmp_path` state, and a controllable clock.

```python
def test_sample_produces_ok_reading():          # snapshot: status "ok", total_gal approx 177255.48 abs 0.04, last_error None
def test_capture_failure_keeps_last_total():    # good sample, then grab raises: last_error startswith "capture", total unchanged, status "ok"
def test_goes_stale_after_five_minutes():       # good sample, clock +6 min, failures only -> status "stale", total still present
def test_error_before_first_reading():          # only failures, no state file -> status "error", total_gal None
def test_ir_frame_sets_needle_error():          # grab returns IR fixture -> last_error == "needle not found"
def test_restart_restores_state():              # sample, new MeterService on same state_path -> snapshot total equals previous, status "ok"

# test_api.py, create_app(service, run_loop=False) with TestClient
def test_reading_endpoint():   # GET /reading 200 JSON with keys total_gal, flow_gpm, continuous_flow_minutes, status, last_success, last_error; last_success ends with "Z"
def test_debug_before_sample():  # GET /debug.jpg 404
def test_debug_after_sample():   # GET /debug.jpg 200, content-type image/jpeg
def test_healthz():              # GET /healthz 200 even when every grab fails
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_service.py tests/test_api.py -v`
Expected: FAIL, `ImportError`.

- [ ] **Step 3: Implement `capture.py`, `service.py`, `api.py`, `__main__.py`**

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest -v`
Expected: all pass.

- [ ] **Step 5: Live check against the camera**

Run: `RTSP_URL='rtsp://camera:two3four@192.168.1.48:554/stream1' CONFIG_PATH=config.yaml STATE_PATH=/tmp/opencode-wm-state.json MODEL_PATH=models/dig-class100-0182-s2_q.tflite .venv/bin/python -m water_meter` in the background, wait 40 s, then `curl -s localhost:8080/reading` and `curl -s -o /tmp/opencode-debug.jpg localhost:8080/debug.jpg`.
Expected: `status` `ok`, `total_gal` near 177255-177300, overlay aligned when viewed. Stop the process.

- [ ] **Step 6: Commit**

```bash
git add water-meter/app
git commit -m "add water meter capture loop and http api"
```

---

### Task 8: Container image and CI

**Files:**
- Create: `water-meter/app/Dockerfile`, `water-meter/app/.dockerignore`, `.github/workflows/water-meter.yml`

**Interfaces:**
- Produces: `ghcr.io/gammons/water-meter:sha-<short-sha>` and `:latest`.

Dockerfile: `python:3.12-slim`, install `requirements.txt`, copy `water_meter/`, `models/` to `/app`, run as non-root UID 10001, `EXPOSE 8080`, `CMD ["python", "-m", "water_meter"]`. `.dockerignore` excludes `.venv`, `tests`, `__pycache__`.

Workflow: on push to `master` with paths `water-meter/app/**` and `.github/workflows/water-meter.yml`, plus `workflow_dispatch`. Job permissions `contents: read`, `packages: write`. Steps: checkout; setup-python 3.12; install `requirements-dev.txt`; `pytest` in `water-meter/app`; `docker/login-action` to `ghcr.io` with `GITHUB_TOKEN`; `docker/metadata-action` for tags `latest` and `type=sha,prefix=sha-`; `docker/build-push-action` with context `water-meter/app`, platform `linux/amd64`, push true.

- [ ] **Step 1: Build and smoke-test locally**

Run: `docker build -t water-meter:local water-meter/app && docker run --rm -d --name wm -p 18080:8080 -e RTSP_URL='rtsp://camera:two3four@192.168.1.48:554/stream1' -e CONFIG_PATH=/cfg/config.yaml -e STATE_PATH=/tmp/state.json -v $PWD/water-meter/app/config.yaml:/cfg/config.yaml:ro water-meter:local`, wait 40 s, `curl -s localhost:18080/reading`, then `docker stop wm`.
Expected: `status` `ok`.

- [ ] **Step 2: Commit and push**

```bash
git add water-meter/app/Dockerfile water-meter/app/.dockerignore .github/workflows/water-meter.yml
git commit -m "build and publish the water meter image to ghcr"
git push origin master
```

- [ ] **Step 3: Verify the workflow**

Run: `gh run watch --repo gammons/kube-cluster $(gh run list --repo gammons/kube-cluster --workflow water-meter.yml --limit 1 --json databaseId -q '.[0].databaseId') --exit-status`
Expected: success.

- [ ] **Step 4: Make the package public (user action)**

Ask the user to open `https://github.com/users/gammons/packages/container/water-meter/settings` and set visibility to Public. Verify with `docker logout ghcr.io; docker pull ghcr.io/gammons/water-meter:latest` succeeding.

---

### Task 9: Kubernetes deployment

**Files:**
- Create: `water-meter/ns.yml`, `pvc.yml`, `configmap.yml`, `secret.example.yml`, `deployment.yml`, `service.yml`, `README.md`
- Modify: `README.md` (root: add a "Water meter" entry pointing at `water-meter/README.md`, like CLIProxyAPI)

**Interfaces:**
- Consumes: image tag from Task 8; `config.yaml` from Task 4.
- Produces: `http://water-meter.water-meter.svc.cluster.local:8080`.

Details: `configmap.yml` is ConfigMap `water-meter-config` with key `config.yaml` holding the exact content of `water-meter/app/config.yaml`, mounted at `/config`. Secret `water-meter-rtsp` key `url`; `secret.example.yml` uses `rtsp://USER:PASSWORD@192.168.1.48:554/stream1`; the real `secret.yml` uses the actual credentials and is not committed. Deployment `water-meter` per Global Constraints, `strategy: Recreate`, liveness and readiness `GET /healthz :8080`, PVC mounted at `/state`, pod `securityContext.fsGroup: 10001`. README covers: apply order, recalibration (calibrate CLI, update both `config.yaml` and `configmap.yml`, `kubectl rollout restart`), bucket test, and troubleshooting (`/debug.jpg` via `kubectl port-forward`, IR mode, camera IP reservation).

- [ ] **Step 1: Write the manifests and README**

- [ ] **Step 2: Validate**

Run: `kubectl --context local-k3s apply --dry-run=server -f water-meter/ns.yml` then the same for the rest after the namespace exists.
Expected: no errors.

- [ ] **Step 3: Apply**

Run: `kubectl --context local-k3s apply -f water-meter/ns.yml -f water-meter/pvc.yml -f water-meter/configmap.yml -f water-meter/secret.yml -f water-meter/deployment.yml -f water-meter/service.yml` then `kubectl --context local-k3s -n water-meter rollout status deploy/water-meter --timeout=180s`.
Expected: rollout complete.

- [ ] **Step 4: Verify in-cluster**

Run: `kubectl --context local-k3s -n home-assistant exec home-assistant-0 -- curl -s http://water-meter.water-meter.svc.cluster.local:8080/reading` (fall back to `python3 -c "import urllib.request..."` if curl is absent).
Expected: JSON with `status` `ok`. This proves both the camera route from the cluster and the HA-to-service path.

- [ ] **Step 5: Commit**

```bash
git add water-meter README.md
git commit -m "deploy the water meter reader to k3s"
```

---

### Task 10: Home Assistant package and alerts

**Files:**
- Create: `home-assistant/water_meter.yaml`
- Modify: `home-assistant/README.md` (new "Water meter (2026-10-01)" section in the Envisalink style)
- Live: `/config/water_meter.yaml` and `/config/configuration.yaml` in pod `home-assistant-0`

**Interfaces:**
- Consumes: `/reading` from Task 9.
- Produces: entities `sensor.water_meter_total`, `sensor.water_flow`, `sensor.water_continuous_flow`, `sensor.water_meter_status`; automations `water_slow_leak`, `water_large_flow`, `water_meter_unreadable`.

Package content:
- `rest:` one resource, `scan_interval: 30`, four sensors with the spec's units and classes and `unique_id`s equal to the entity object IDs above. `total_gal` sensor: `unit_of_measurement: gal`, `device_class: water`, `state_class: total_increasing`; it must be unavailable rather than 0 when `total_gal` is null.
- Automations, each `action: notify.mobile_app_pixel_10_pro`:
  - `water_slow_leak`: numeric_state `sensor.water_continuous_flow` above 120. Title "Possible water leak"; message "Water has been flowing continuously for {{ states('sensor.water_continuous_flow') }} minutes. Check toilets and hoses."
  - `water_large_flow`: numeric_state `sensor.water_flow` above 1.0 for 20 minutes. Title "Heavy water use"; message "Water has been flowing at over 1 gal/min for 20 minutes ({{ states('sensor.water_flow') }} gal/min now)."
  - `water_meter_unreadable`: template `{{ states('sensor.water_meter_status') != 'ok' }}` for 30 minutes. Title "Water meter unreadable"; message "The water meter camera has not produced a good reading for 30 minutes (status: {{ states('sensor.water_meter_status') }})."

- [ ] **Step 1: Write `home-assistant/water_meter.yaml`**

- [ ] **Step 2: Install on the HA volume**

Back up first: `kubectl --context local-k3s -n home-assistant exec home-assistant-0 -- cp /config/configuration.yaml /config/configuration.yaml.bak-water-meter`. Copy the package with `kubectl cp`. Append to `configuration.yaml`:

```yaml
homeassistant:
  packages:
    water_meter: !include water_meter.yaml
```

- [ ] **Step 3: Check config and restart**

Run: `kubectl --context local-k3s -n home-assistant exec home-assistant-0 -- python -m homeassistant --script check_config -c /config`
Expected: no errors mentioning `water_meter`. Then `kubectl --context local-k3s -n home-assistant delete pod home-assistant-0` and wait for Ready.

- [ ] **Step 4: Verify entities**

Ask the user to confirm in HA that the four sensors have values (total about 177,256 gal, status `ok`) and to add `sensor.water_meter_total` under Settings → Dashboards → Energy → Water consumption. Also confirm `notify.mobile_app_pixel_10_pro` exists by sending a test notification from Developer Tools → Actions.

- [ ] **Step 5: Commit**

```bash
git add home-assistant/water_meter.yaml home-assistant/README.md
git commit -m "add home assistant water meter sensors and leak alerts"
```

---

### Task 11: Acceptance with the user

No code. Each check is done with the user present; record results in `water-meter/README.md` under "Acceptance (2026-10-xx)".

- [ ] **Step 1: Bucket test** — user runs exactly 1 gallon into a bucket; `/reading` total increases by 1.0 ± 0.05 and the needle makes one revolution. If not, fix `wheel_units` / `needle_units_per_rev` in both config files and redeploy.
- [ ] **Step 2: Large flow** — user runs a tap for a minute; `flow_gpm` rises above 1 and readings stay accepted (`last_error` not `rejected`).
- [ ] **Step 3: Slow leak signal** — user leaves a tap barely dripping for 20 minutes; `continuous_flow_minutes` rises past 15 and keeps climbing. (The 2-hour alert itself is exercised by temporarily lowering the HA threshold to 15, then restoring 120.)
- [ ] **Step 4: Unreadable** — confirm `status` goes `stale` after 5 minutes with the camera unplugged; the HA alert test uses a temporary 1-minute `for`, then restore 30.
- [ ] **Step 5: Commit** the README acceptance notes.

```bash
git add water-meter/README.md
git commit -m "record water meter acceptance results"
```
