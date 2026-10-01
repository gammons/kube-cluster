import pytest

from water_meter.combine import InconsistentReading, resolve_reading
from water_meter.config import Tuning

T = Tuning()

# One needle revolution advances the last wheel by one digit (0.1 gal).
# Raw wheel values below are what the model returned on real frames from the camera.


def test_units_are_tenths():
    assert T.wheel_units == 0.1
    assert T.needle_units_per_rev == 0.1


def test_quiet_reading():  # flow_177267_225.jpg
    assert resolve_reading([0.1, 1, 7, 7, 2.1, 5.9, 7.0, 2.3], 0.247, T) == pytest.approx(177267.2247)


def test_after_one_gallon():  # flow_177268_253.jpg: wheel 6 reads 7.9 but has rolled to 8
    assert resolve_reading([0.1, 1, 7, 7, 2.1, 5.9, 7.9, 2.5], 0.534, T) == pytest.approx(177268.2534)


def test_just_after_carry():  # flow_177268_004_midroll.jpg
    assert resolve_reading([0.1, 1, 7, 7, 2.1, 5.9, 7.9, 0.0], 0.035, T) == pytest.approx(177268.0035)


def test_just_before_carry():  # flow_177267_905.jpg: wheel 6 already reads 7.1
    assert resolve_reading([0.1, 1, 7, 7, 2.1, 5.9, 7.1, 9.0], 0.050, T) == pytest.approx(177267.905)


def test_needle_near_full_turn_uses_lower_digit():
    assert resolve_reading([0.1, 1, 7, 7, 2.1, 5.9, 7.9, 2.0], 0.983, T) == pytest.approx(177268.1983)


def test_cascade_before():
    assert resolve_reading([0, 1, 7, 7.5, 9.5, 9.6, 9.7, 9.8], 0.96, T) == pytest.approx(177999.996)


def test_cascade_after():
    assert resolve_reading([0, 1, 7, 8.0, 0.0, 0.0, 0.0, 0.1], 0.02, T) == pytest.approx(178000.002)


def test_tolerance_is_in_last_wheel_digits():
    with pytest.raises(InconsistentReading):
        resolve_reading([0.1, 1, 7, 7, 2.1, 5.9, 7.0, 2.0], 0.5, T)
