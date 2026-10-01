import pytest

from water_meter.combine import InconsistentReading, resolve_reading
from water_meter.config import Tuning

T = Tuning()


def test_plain_reading():
    assert resolve_reading([0, 1, 7, 7, 2, 5, 5.0, 4.8], 0.48, T) == pytest.approx(177255.48)


def test_misread_neighbour_rounds():
    assert resolve_reading([0, 1, 7, 7, 2, 5, 4.8, 5.4], 0.48, T) == pytest.approx(177255.48)


def test_just_rolled():
    assert resolve_reading([0, 1, 7, 7, 2, 5, 5.8, 0.3], 0.02, T) == pytest.approx(177256.02)


def test_mid_roll_not_yet():
    assert resolve_reading([0, 1, 7, 7, 2, 5, 5.4, 9.5], 0.95, T) == pytest.approx(177255.95)


def test_cascade_before():
    assert resolve_reading([0, 1, 7, 7.5, 9.5, 9.6, 9.5, 9.6], 0.96, T) == pytest.approx(177999.96)


def test_cascade_after():
    assert resolve_reading([0, 1, 7, 8.0, 0.0, 0.0, 0.1, 0.2], 0.01, T) == pytest.approx(178000.01)


def test_last_wheel_wraps_ok():
    assert resolve_reading([0, 1, 7, 7, 2, 5, 6.0, 9.8], 0.01, T) == pytest.approx(177256.01)


def test_inconsistent_raises():
    with pytest.raises(InconsistentReading):
        resolve_reading([0, 1, 7, 7, 2, 5, 5, 8.0], 0.48, T)
