import json
import logging
import os
import tempfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

log = logging.getLogger(__name__)


@dataclass
class PersistedState:
    last_total: float
    last_ts: datetime
    streak_start: datetime | None


def _parse_ts(value) -> datetime:
    ts = datetime.fromisoformat(value)
    if ts.tzinfo is None:
        raise ValueError("timestamp without timezone")
    return ts


def load_state(path: Path) -> PersistedState | None:
    path = Path(path)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text())
        return PersistedState(
            last_total=float(data["last_total"]),
            last_ts=_parse_ts(data["last_ts"]),
            streak_start=_parse_ts(data["streak_start"]) if data.get("streak_start") else None,
        )
    except (OSError, ValueError, KeyError, TypeError) as exc:
        log.warning("ignoring unreadable state file %s: %s", path, exc)
        return None


def save_state(path: Path, state: PersistedState) -> None:
    path = Path(path)
    payload = {
        "last_total": state.last_total,
        "last_ts": state.last_ts.isoformat(),
        "streak_start": state.streak_start.isoformat() if state.streak_start else None,
    }
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".state-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as fh:
            json.dump(payload, fh)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
