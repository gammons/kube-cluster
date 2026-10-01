import pytest
from fastapi.testclient import TestClient

from tests.conftest import warm
from water_meter.api import create_app


@pytest.fixture
def client_for(make_service):
    def build():
        service = make_service()
        return service, TestClient(create_app(service, run_loop=False))

    return build


def test_reading_endpoint(client_for, clock):
    service, client = client_for()
    warm(service, clock)
    response = client.get("/reading")
    assert response.status_code == 200
    body = response.json()
    assert set(body) == {"total_gal", "flow_gpm", "continuous_flow_minutes", "status", "last_success", "last_error"}
    assert body["status"] == "ok"
    assert body["last_success"].endswith("Z")


def test_debug_before_sample(client_for):
    _, client = client_for()
    assert client.get("/debug.jpg").status_code == 404


def test_debug_after_sample(client_for):
    service, client = client_for()
    service.sample()
    response = client.get("/debug.jpg")
    assert response.status_code == 200
    assert response.headers["content-type"] == "image/jpeg"


def test_healthz(client_for, camera):
    camera.fail = True
    service, client = client_for()
    service.sample()
    assert client.get("/healthz").status_code == 200
