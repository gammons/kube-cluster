from pathlib import Path

import cv2
import pytest

from water_meter.combine import resolve_reading
from water_meter.config import Tuning, load_config
from water_meter.digits import DigitReader
from water_meter.needle import read_needle
from water_meter.regions import deskew, digit_crops

APP = Path(__file__).resolve().parent.parent
MODEL = APP / "models" / "dig-class100-0182-s2_q.tflite"
EXPECTED = [0, 1, 7, 7, 2, 5, 5]


@pytest.fixture(scope="module")
def reader():
    return DigitReader(MODEL)


@pytest.fixture(scope="module")
def meter():
    cal = load_config(APP / "config.yaml").calibration
    return cal, deskew(cv2.imread(str(APP / "tests" / "fixtures" / "color_177255.jpg")), cal)


def dial_distance(a, b):
    d = abs(a - b) % 10
    return min(d, 10 - d)


def test_model_shapes(reader):
    assert list(reader.input_shape) == [1, 32, 20, 3]
    assert list(reader.output_shape) == [1, 100]


def test_real_frame_digits(reader, meter):
    cal, image = meter
    results = [reader.read(c) for c in digit_crops(image, cal)]
    for result, expected in zip(results[:7], EXPECTED):
        assert dial_distance(result.value, expected) <= 0.3, results
        assert result.confidence >= 0.6, results


def test_real_frame_resolves(reader, meter):
    cal, image = meter
    values = [reader.read(c).value for c in digit_crops(image, cal)]
    total = resolve_reading(values, read_needle(image, cal), Tuning())
    assert total == pytest.approx(177255.5496, abs=0.005)
