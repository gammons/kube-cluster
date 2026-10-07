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


def test_impossible_jump_never_accepted_however_long_it_persists():
    p = seeded()
    # 400 gal appearing 15 s after the last reading; persists for 30 minutes, long past
    # the 16 minutes in which 25 gal/min could cover 400 gal.
    results = feed(p, [500.0 + 0.001 * i for i in range(120)])
    assert all(r is None for r in results)
    assert p.last_total == 100.0


def test_wheel_misread_replay_2026_10_05():
    # Hundreds wheel read as 7 instead of 3 for half an hour while ~0.35 gal/min flowed.
    last_good = datetime(2026, 10, 5, 22, 35, 58, tzinfo=timezone.utc)
    p = Plausibility(Tuning(), last_total=178299.537, last_ts=last_good)
    step = timedelta(seconds=17)
    start = last_good + timedelta(seconds=27)
    misreads = [p.check(178700.417 + 0.1 * i, start + i * step) for i in range(105)]
    assert all(r is None for r in misreads)
    assert p.last_total == 178299.537

    after = start + 105 * step
    truth = [p.check(178310.25 + 0.001 * i, after + i * step) for i in range(3)]
    assert truth == [None, None, pytest.approx(178310.252)]


def test_impossible_jump_stays_rejected_after_stray_reading_2026_10_07():
    # The +202 gal jump appeared 15 s after the last accepted reading. After an hour of
    # rejections one stray reading broke the run, and the same value then got accepted
    # as if the camera had been offline.
    last_good = datetime(2026, 10, 7, 13, 14, 20, tzinfo=timezone.utc)
    p = Plausibility(Tuning(), last_total=178399.51, last_ts=last_good)
    step = timedelta(seconds=17)
    t = last_good + timedelta(seconds=15)
    results = []
    for i in range(200):
        results.append(p.check(178601.6 + 0.0001 * i, t))
        t += step
    results.append(p.check(178501.3, t))
    for i in range(10):
        t += step
        results.append(p.check(178601.65 + 0.0001 * i, t))
    assert all(r is None for r in results)
    assert p.last_total == 178399.51


def test_backwards_recovery_after_consistent_rejections():
    p = Plausibility(Tuning(), last_total=7777777.384, last_ts=T0)
    results = feed(p, [177714.0 + i * 0.001 for i in range(42)])
    assert all(r is None for r in results[:40])
    assert results[-1] is not None
    assert 177714.0 <= p.last_total <= 177714.1


def test_backwards_recovery_while_water_flows():
    p = Plausibility(Tuning(), last_total=500.0, last_ts=T0)
    results = feed(p, [100.0 + 0.05 * i for i in range(46)])
    assert any(r is not None for r in results), "a 0.2 gal/min stream of consistent readings should recover"


def test_large_jump_after_outage_accepted():
    p = seeded()
    back = T0 + timedelta(hours=2)
    results = [p.check(300.0 + 0.01 * i, back + i * STEP) for i in range(3)]
    assert results == [None, None, pytest.approx(300.02)]


def test_no_recovery_when_rejections_disagree():
    p = seeded()
    assert all(r is None for r in feed(p, [500.0 if i % 2 else 900.0 for i in range(44)]))
    assert p.last_total == 100.0
