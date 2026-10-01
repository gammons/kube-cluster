from dataclasses import replace
from pathlib import Path

import cv2
import numpy as np

from water_meter.config import load_config
from water_meter.debug import render
from water_meter.regions import deskew, digit_crops

APP = Path(__file__).resolve().parent.parent
CAL = load_config(APP / "config.yaml").calibration


def test_deskew_returns_meter_crop_size():
    frame = np.zeros((1296, 2304, 3), np.uint8)
    meter = deskew(frame, CAL)
    assert meter.shape == (CAL.meter_crop[3], CAL.meter_crop[2], 3)


def test_deskew_rotates_about_crop_centre():
    frame = np.zeros((1296, 2304, 3), np.uint8)
    x, y, w, h = CAL.meter_crop
    cv2.line(frame, (x + w // 2 - 100, y + h // 2), (x + w // 2 + 100, y + h // 2), (255, 255, 255), 3)
    meter = deskew(frame, replace(CAL, rotation_deg=90))
    column = meter[:, w // 2]
    assert column.max() == 255 and meter[h // 2, w // 2 - 50].max() < 255


def test_digit_crops_follow_boxes():
    meter = np.zeros((CAL.meter_crop[3], CAL.meter_crop[2], 3), np.uint8)
    crops = digit_crops(meter, CAL)
    assert len(crops) == 8
    for crop, (_, _, w, h) in zip(crops, CAL.digit_boxes):
        assert crop.shape == (h, w, 3)


def test_render_keeps_size():
    meter = np.full((CAL.meter_crop[3], CAL.meter_crop[2], 3), 255, np.uint8)
    out = render(meter, CAL, digit_values=[1.0] * 8, needle=0.25, total=123.456)
    assert out.shape == meter.shape
    assert not np.array_equal(out, meter)
