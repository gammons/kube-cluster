from datetime import datetime, timedelta, timezone

import pytest

from water_meter.combine import Plausibility
from water_meter.config import Tuning

T0 = datetime(2026, 10, 1, 20, 0, tzinfo=timezone.utc)
STEP = timedelta(seconds=15)


def seeded():
    return Plausibility(Tuning(), last_total=100.0, last_ts=T0)


def feed(p, values, start=T0):
    return [p.check(v, start + (i + 1) * STEP) for i, v in enumerate(values)]


def test_first_reading_needs_confirmation():
    p = Plausibility(Tuning())
    assert feed(p, [100.0, 100.0, 100.01]) == [None, None, 100.01]
    assert p.last_total == 100.01


def test_first_reading_misread_not_accepted():
    p = Plausibility(Tuning())
    results = feed(p, [977263.0, 177263.0, 177263.01, 177263.02])
    assert results == [None, None, None, 177263.02]


def test_small_backwards_clamped():
    assert seeded().check(99.99, T0 + STEP) == 100.0


def test_small_increase_accepted_immediately():
    assert seeded().check(100.9, T0 + STEP) == pytest.approx(100.9)


def test_large_backwards_rejected():
    p = seeded()
    assert p.check(99.5, T0 + STEP) is None
    assert p.last_total == 100.0
    assert p.last_rejection is not None


def test_over_max_rejected():
    assert seeded().check(100.0 + 25 * 0.25 + 0.03, T0 + STEP) is None


def test_heavy_flow_confirmed_after_three_samples():
    p = seeded()
    assert feed(p, [101.5, 103.0, 104.5]) == [None, None, pytest.approx(104.5)]


def test_misread_after_gap_rejected():
    p = seeded()
    assert p.check(110.0, T0 + timedelta(minutes=15)) is None
    assert p.check(100.5, T0 + timedelta(minutes=15) + STEP) == pytest.approx(100.5)


def test_recovery_after_consistent_rejections():
    p = seeded()
    results = feed(p, [500.0 + i * 0.001 for i in range(42)])
    assert all(r is None for r in results[:40])
    assert results[-1] is not None
    assert 500.0 <= p.last_total <= 500.1


def test_recovery_while_water_flows():
    p = seeded()
    results = feed(p, [500.0 + 0.05 * i for i in range(46)])
    assert any(r is not None for r in results), "a 0.2 gal/min stream of consistent readings should recover"


def test_no_recovery_when_rejections_disagree():
    p = seeded()
    assert all(r is None for r in feed(p, [500.0 if i % 2 else 900.0 for i in range(44)]))
    assert p.last_total == 100.0
