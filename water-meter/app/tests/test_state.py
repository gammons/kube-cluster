from datetime import datetime, timezone

import pytest

from water_meter.state import PersistedState, load_state, save_state

T0 = datetime(2026, 10, 1, 20, 0, tzinfo=timezone.utc)


@pytest.mark.parametrize("streak", [None, datetime(2026, 10, 1, 18, 30, tzinfo=timezone.utc)])
def test_round_trip(tmp_path, streak):
    path = tmp_path / "state.json"
    state = PersistedState(last_total=177255.48, last_ts=T0, streak_start=streak)
    save_state(path, state)
    loaded = load_state(path)
    assert loaded == state
    assert loaded.last_ts.tzinfo is not None


def test_load_missing_returns_none(tmp_path):
    assert load_state(tmp_path / "state.json") is None


@pytest.mark.parametrize("content", ["", '{"last_total": 177255.4', "[]", '{"last_total": "x"}'])
def test_load_state_corrupt_returns_none(tmp_path, content):
    path = tmp_path / "state.json"
    path.write_text(content)
    assert load_state(path) is None


def test_save_is_atomic(tmp_path):
    path = tmp_path / "state.json"
    save_state(path, PersistedState(last_total=1.0, last_ts=T0, streak_start=None))
    save_state(path, PersistedState(last_total=2.0, last_ts=T0, streak_start=None))
    assert [p.name for p in tmp_path.iterdir()] == ["state.json"]
