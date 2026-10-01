from datetime import datetime, timedelta, timezone
from pathlib import Path

import cv2
import pytest

from water_meter.capture import CaptureError
from water_meter.config import load_config
from water_meter.digits import DigitReader
from water_meter.service import MeterService

APP = Path(__file__).resolve().parent.parent
FIXTURES = APP / "tests" / "fixtures"


class Clock:
    def __init__(self):
        self.now = datetime(2026, 10, 1, 21, 0, tzinfo=timezone.utc)

    def __call__(self):
        return self.now

    def advance(self, **kwargs):
        self.now += timedelta(**kwargs)


class FakeCamera:
    def __init__(self):
        self.frame = cv2.imread(str(FIXTURES / "color_177255.jpg"))
        self.fail = False

    def use(self, name):
        self.frame = cv2.imread(str(FIXTURES / name))

    def __call__(self):
        if self.fail:
            raise CaptureError("connection refused")
        return self.frame.copy()


@pytest.fixture(scope="session")
def reader():
    return DigitReader(APP / "models" / "dig-class100-0182-s2_q.tflite")


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def camera():
    return FakeCamera()


@pytest.fixture
def make_service(reader, clock, camera, tmp_path):
    config = load_config(APP / "config.yaml")

    def build():
        return MeterService(config, reader, camera, tmp_path / "state.json", clock)

    return build
