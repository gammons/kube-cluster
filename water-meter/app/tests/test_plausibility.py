from datetime import datetime, timedelta, timezone

import pytest

from water_meter.combine import Plausibility
from water_meter.config import Tuning

T0 = datetime(2026, 10, 1, 20, 0, tzinfo=timezone.utc)
STEP = timedelta(seconds=15)


def seeded():
    return Plausibility(Tuning(), last_total=100.0, last_ts=T0)


def test_first_reading_accepted():
    p = Plausibility(Tuning())
    assert p.check(100.0, T0) == 100.0
    assert p.last_total == 100.0
    assert p.last_ts == T0


def test_small_backwards_clamped():
    assert seeded().check(99.99, T0 + STEP) == 100.0


def test_large_backwards_rejected():
    p = seeded()
    assert p.check(99.5, T0 + STEP) is None
    assert p.last_total == 100.0
    assert p.last_rejection is not None


def test_over_max_rejected():
    assert seeded().check(100.0 + 25 * 0.25 + 0.03, T0 + STEP) is None


def test_at_max_accepted():
    value = 100.0 + 25 * 0.25
    assert seeded().check(value, T0 + STEP) == pytest.approx(value)


def test_recovery_after_consistent_rejections():
    p = seeded()
    results = []
    for i in range(1, 43):
        ts = T0 + i * STEP
        results.append((ts, p.check(500.0 + (i % 5) * 0.1, ts)))
    first_ts = results[0][0]
    for ts, value in results:
        if ts - first_ts < timedelta(minutes=10):
            assert value is None
    accepted = [v for _, v in results if v is not None]
    assert accepted, "a consistent new value should be accepted after 10 minutes"
    assert 500.0 <= p.last_total <= 500.5


def test_no_recovery_when_rejections_disagree():
    p = seeded()
    for i in range(1, 45):
        value = 500.0 if i % 2 else 900.0
        assert p.check(value, T0 + i * STEP) is None
    assert p.last_total == 100.0
