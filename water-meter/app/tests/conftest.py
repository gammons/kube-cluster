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


def load(name):
    return cv2.imread(str(FIXTURES / name))


class FakeCamera:
    """Returns a burst of frames; by default five copies of one fixture."""

    def __init__(self):
        self.frames = [load("color_177255.jpg")] * 5
        self.fail = False
        self.on_grab = None

    def use(self, *names):
        self.frames = [load(n) for n in names] if len(names) > 1 else [load(names[0])] * 5

    def __call__(self):
        if self.on_grab:
            self.on_grab()
        if self.fail:
            raise CaptureError("connection refused")
        return [f.copy() for f in self.frames]


@pytest.fixture(scope="session")
def reader():
    return DigitReader(APP / "models" / "dig-class100-0182-s2_q.tflite")


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def camera():
    return FakeCamera()


def warm(service, clock, samples=3):
    for i in range(samples):
        if i:
            clock.advance(seconds=15)
        service.sample()


@pytest.fixture
def make_service(reader, clock, camera, tmp_path):
    config = load_config(APP / "config.yaml")

    def build():
        return MeterService(config, reader, camera, tmp_path / "state.json", clock)

    return build
