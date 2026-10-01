import math
from pathlib import Path

import cv2
import numpy as np
import pytest

from water_meter.config import Calibration, load_config
from water_meter.needle import read_needle
from water_meter.regions import deskew

APP = Path(__file__).resolve().parent.parent
FIXTURES = APP / "tests" / "fixtures"
RED = (40, 40, 220)

SYNTH = Calibration(
    rotation_deg=0,
    meter_crop=(0, 0, 200, 200),
    digit_boxes=[(0, 0, 10, 10)] * 8,
    dial_center=(100.0, 100.0),
    hub_radius=8,
    rim_radius=90,
    zero_angle_deg=0,
)


def synthetic_dial(angle_deg: float) -> np.ndarray:
    img = np.full((200, 200, 3), 255, np.uint8)
    a = math.radians(angle_deg)
    tip = (int(round(100 + 80 * math.sin(a))), int(round(100 - 80 * math.cos(a))))
    cv2.line(img, (100, 100), tip, RED, 3)
    cv2.circle(img, (100, 100), 8, RED, -1)
    return img


@pytest.mark.parametrize("deg", [0, 45, 90, 180, 270, 300])
def test_synthetic_angles(deg):
    assert read_needle(synthetic_dial(deg), SYNTH) == pytest.approx(deg / 360, abs=0.005)


def test_needle_wraparound_near_zero():
    value = read_needle(synthetic_dial(359.5), SYNTH)
    assert value < 0.003 or value > 0.997


def test_zero_angle_offset_applied():
    cal = Calibration(**{**SYNTH.__dict__, "zero_angle_deg": 90.0})
    assert read_needle(synthetic_dial(180), cal) == pytest.approx(0.25, abs=0.005)


def test_no_red_returns_none():
    assert read_needle(np.full((200, 200, 3), 255, np.uint8), SYNTH) is None


def test_hub_only_returns_none():
    img = np.full((200, 200, 3), 255, np.uint8)
    cv2.circle(img, (100, 100), 8, RED, -1)
    assert read_needle(img, SYNTH) is None


def test_real_frame():
    cal = load_config(APP / "config.yaml").calibration
    meter = deskew(cv2.imread(str(FIXTURES / "color_177255.jpg")), cal)
    assert read_needle(meter, cal) == pytest.approx(0.485, abs=0.03)


def test_ir_frame_has_no_needle():
    cal = load_config(APP / "config.yaml").calibration
    meter = deskew(cv2.imread(str(FIXTURES / "ir.jpg")), cal)
    assert read_needle(meter, cal) is None


def test_pinkish_needle_found():
    cal = load_config(APP / "config.yaml").calibration
    meter = deskew(cv2.imread(str(FIXTURES / "flow_pink_needle.jpg")), cal)
    assert read_needle(meter, cal) == pytest.approx(0.53, abs=0.02)
