import pytest

from tests.conftest import warm

FIXTURE_TOTAL = 177255.5496


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
