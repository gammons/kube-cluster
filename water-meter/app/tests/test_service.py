import pytest


def test_sample_produces_ok_reading(make_service):
    service = make_service()
    service.sample()
    snap = service.snapshot()
    assert snap["status"] == "ok"
    assert snap["total_gal"] == pytest.approx(177255.48, abs=0.04)
    assert snap["last_error"] is None
    assert snap["flow_gpm"] == 0.0
    assert snap["continuous_flow_minutes"] == 0


def test_capture_failure_keeps_last_total(make_service, camera, clock):
    service = make_service()
    service.sample()
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
    service.sample()
    camera.fail = True
    for _ in range(24):
        clock.advance(seconds=15)
        service.sample()
    snap = service.snapshot()
    assert snap["status"] == "stale"
    assert snap["total_gal"] == pytest.approx(177255.48, abs=0.04)


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
    first.sample()
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
