import pytest

from tests.conftest import APP, warm
from water_meter.config import Calibration, Config, Tuning, load_config
from water_meter.service import MeterService

FIXTURE_TOTAL = 177255.5496

# Calibration as of 2026-10-07 with wheel 4's box one pixel further right, on the dark
# edge of the next wheel's window. On night_178637.jpg the model then reads that wheel's
# 6 as a confident 1 (0.9), as happened live with its 5 overnight.
EDGE_CALIBRATION = Calibration(
    rotation_deg=12.0,
    meter_crop=(830, 600, 420, 420),
    digit_boxes=[(60, 104, 20, 32), (90, 104, 20, 32), (121, 104, 20, 32), (154, 104, 20, 32),
                 (192, 104, 20, 32), (222, 104, 20, 32), (255, 104, 20, 32), (289, 104, 20, 32)],
    dial_center=(242.0, 242.0),
    hub_radius=11.0,
    rim_radius=78.0,
    zero_angle_deg=0.0,
)


def test_sample_produces_ok_reading(make_service, clock):
    service = make_service()
    warm(service, clock)
    snap = service.snapshot()
    assert snap["status"] == "ok"
    assert snap["total_gal"] == pytest.approx(FIXTURE_TOTAL, abs=0.005)
    assert snap["last_error"] is None
    assert snap["flow_gpm"] == 0.0


def test_first_sample_awaits_confirmation(make_service):
    service = make_service()
    service.sample()
    snap = service.snapshot()
    assert snap["total_gal"] is None
    assert snap["last_error"].startswith("rejected: awaiting confirmation")


def test_burst_outvotes_single_misread(make_service, camera, clock):
    # last wheel misread 5 -> 3 on one frame of the burst (seen live while water ran)
    camera.use("flow_177268_253.jpg", "flow_177268_253.jpg", "flow_last_wheel_misread.jpg",
               "flow_177268_253.jpg", "flow_177268_253.jpg")
    service = make_service()
    warm(service, clock)
    assert service.snapshot()["total_gal"] == pytest.approx(177268.2534, abs=0.003)


def test_burst_disagreement_rejected(make_service, camera):
    camera.use("flow_177267_225.jpg", "flow_177268_253.jpg", "flow_last_wheel_misread.jpg",
               "flow_177267_905.jpg", "flow_177268_004_midroll.jpg")
    service = make_service()
    service.sample()
    assert service.snapshot()["last_error"] == "frames disagree"


def test_wheel_box_on_window_edge_rejected_not_misread(reader, camera, clock, tmp_path):
    camera.use("night_178637.jpg")
    service = MeterService(Config(EDGE_CALIBRATION, Tuning()), reader, camera, tmp_path / "state.json", clock)
    warm(service, clock)
    snap = service.snapshot()
    assert snap["total_gal"] is None
    assert snap["last_error"] == "unstable digit: wheel 4"


@pytest.mark.parametrize(
    "frame, total",
    [
        ("color_177713.jpg", 177713.505),  # daylight, 2026-10-03
        ("night_178637.jpg", 178637.951),  # spotlight, 2026-10-07; image sits ~3 px further right
    ],
)
def test_live_config_reads_day_and_night_frames(reader, camera, clock, tmp_path, frame, total):
    camera.use(frame)
    config = load_config(APP / "config.yaml")
    service = MeterService(config, reader, camera, tmp_path / "state.json", clock)
    warm(service, clock)
    snap = service.snapshot()
    assert snap["last_error"] is None
    assert snap["total_gal"] == pytest.approx(total, abs=0.01)


def test_capture_failure_keeps_last_total(make_service, camera, clock):
    service = make_service()
    warm(service, clock)
    good = service.snapshot()["total_gal"]
    camera.fail = True
    clock.advance(seconds=15)
    service.sample()
    snap = service.snapshot()
    assert snap["last_error"].startswith("capture")
    assert snap["total_gal"] == good
    assert snap["status"] == "ok"


def test_goes_stale_after_five_minutes(make_service, camera, clock):
    service = make_service()
    warm(service, clock)
    camera.fail = True
    for _ in range(24):
        clock.advance(seconds=15)
        service.sample()
    snap = service.snapshot()
    assert snap["status"] == "stale"
    assert snap["total_gal"] == pytest.approx(FIXTURE_TOTAL, abs=0.005)


def test_error_before_first_reading(make_service, camera):
    camera.fail = True
    service = make_service()
    service.sample()
    snap = service.snapshot()
    assert snap["status"] == "error"
    assert snap["total_gal"] is None
    assert snap["last_success"] is None


def test_ir_frame_sets_needle_error(make_service, camera):
    camera.use("ir.jpg")
    service = make_service()
    service.sample()
    assert service.snapshot()["last_error"] == "needle not found"


def test_restart_restores_state(make_service, camera, clock):
    first = make_service()
    warm(first, clock)
    before = first.snapshot()
    camera.fail = True
    clock.advance(seconds=30)
    second = make_service()
    snap = second.snapshot()
    assert snap["total_gal"] == before["total_gal"]
    assert snap["status"] == "ok"
    assert snap["last_success"] == before["last_success"]


def test_debug_jpeg_after_sample(make_service):
    service = make_service()
    assert service.debug_jpeg() is None
    service.sample()
    assert service.debug_jpeg()[:2] == b"\xff\xd8"
