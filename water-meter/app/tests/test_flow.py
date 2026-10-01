from datetime import datetime, timedelta, timezone

import pytest

from water_meter.config import Tuning
from water_meter.flow import FlowTracker

T0 = datetime(2026, 10, 1, 20, 0, tzinfo=timezone.utc)
STEP = timedelta(seconds=15)


def feed(tracker, start, minutes, gpm, total=100.0, pattern=None):
    """Add a sample every 15 s; returns (last_ts, last_total)."""
    ts = start
    for i in range(int(minutes * 4) + 1):
        ts = start + i * STEP
        value = total + gpm * i / 4 if pattern is None else pattern(i)
        tracker.add(value, ts)
    return ts, value


def test_no_flow_resets_streak():
    f = FlowTracker(Tuning())
    ts, _ = feed(f, T0, 16, 0.0)
    assert f.continuous_flow_minutes(ts) == 0


def test_slow_leak_accumulates():
    f = FlowTracker(Tuning())
    ts, _ = feed(f, T0, 130, 0.02)
    assert f.continuous_flow_minutes(ts) >= 120


def test_sub_tick_noise_is_still():
    f = FlowTracker(Tuning())
    ts, _ = feed(f, T0, 16, 0.0, pattern=lambda i: 100.0 + (0.005 if i % 2 else 0.0))
    assert f.continuous_flow_minutes(ts) == 0


def test_leak_then_quiet():
    f = FlowTracker(Tuning())
    ts, total = feed(f, T0, 60, 0.02)
    ts, _ = feed(f, ts + STEP, 16, 0.0, total=total)
    assert f.continuous_flow_minutes(ts) == 0


def test_flow_gpm():
    f = FlowTracker(Tuning())
    ts, _ = feed(f, T0, 6, 1.5)
    assert f.flow_gpm(ts) == pytest.approx(1.5)


def test_flow_gpm_insufficient_data():
    f = FlowTracker(Tuning())
    f.add(100.0, T0)
    assert f.flow_gpm(T0) == 0.0


def test_streak_survives_restart():
    old = FlowTracker(Tuning())
    ts, total = feed(old, T0, 60, 0.02)
    new = FlowTracker(Tuning(), streak_start=old.streak_start)
    ts, _ = feed(new, ts + STEP, 61, 0.02, total=total)
    assert new.continuous_flow_minutes(ts) >= 120
