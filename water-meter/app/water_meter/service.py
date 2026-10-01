import logging
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

import cv2
import numpy as np

from water_meter.combine import InconsistentReading, Plausibility, resolve_reading
from water_meter.config import Config
from water_meter.debug import render
from water_meter.digits import DigitReader
from water_meter.flow import FlowTracker
from water_meter.needle import read_needle
from water_meter.regions import deskew, digit_crops
from water_meter.state import PersistedState, load_state, save_state

log = logging.getLogger(__name__)


class SampleFailed(Exception):
    pass


def _iso(ts: datetime | None) -> str | None:
    if ts is None:
        return None
    return ts.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class MeterService:
    def __init__(
        self,
        config: Config,
        reader: DigitReader,
        grab: Callable[[], np.ndarray],
        state_path: Path,
        now: Callable[[], datetime] = _utcnow,
    ):
        self._config = config
        self._reader = reader
        self._grab = grab
        self._state_path = Path(state_path)
        self._now = now
        self._lock = threading.Lock()

        state = load_state(self._state_path)
        tuning = config.tuning
        self._plausibility = Plausibility(
            tuning,
            last_total=state.last_total if state else None,
            last_ts=state.last_ts if state else None,
        )
        self._flow = FlowTracker(tuning, streak_start=state.streak_start if state else None)
        self._last_total = state.last_total if state else None
        self._last_success = state.last_ts if state else None
        self._last_error: str | None = None
        self._debug_jpeg: bytes | None = None

    def sample(self) -> None:
        now = self._now()
        try:
            total = self._read_total()
            accepted = self._plausibility.check(total, now)
            if accepted is None:
                raise SampleFailed(f"rejected: {self._plausibility.last_rejection}")
        except SampleFailed as exc:
            log.warning("sample failed: %s", exc)
            with self._lock:
                self._last_error = str(exc)
            return
        except Exception as exc:
            log.exception("unexpected error while sampling")
            with self._lock:
                self._last_error = f"internal: {exc}"
            return

        with self._lock:
            self._flow.add(accepted, now)
            self._last_total = accepted
            self._last_success = now
            self._last_error = None
            state = PersistedState(last_total=accepted, last_ts=now, streak_start=self._flow.streak_start)
        try:
            save_state(self._state_path, state)
        except OSError as exc:
            log.warning("cannot save state: %s", exc)

    def _read_total(self) -> float:
        try:
            frame = self._grab()
        except Exception as exc:
            raise SampleFailed(f"capture: {exc}") from exc

        cal = self._config.calibration
        meter = deskew(frame, cal)
        needle = read_needle(meter, cal)
        results = [self._reader.read(crop) for crop in digit_crops(meter, cal)]
        values = [r.value for r in results]
        self._store_debug(meter, values, needle)

        if needle is None:
            raise SampleFailed("needle not found")
        for i, result in enumerate(results[:-1]):
            if result.confidence < self._config.tuning.min_digit_confidence:
                raise SampleFailed(f"low digit confidence: wheel {i}")
        try:
            total = resolve_reading(values, needle, self._config.tuning)
        except InconsistentReading as exc:
            raise SampleFailed("inconsistent wheels and needle") from exc
        self._store_debug(meter, values, needle, total)
        return total

    def _store_debug(self, meter, values, needle, total=None) -> None:
        image = render(meter, self._config.calibration, values, needle, total)
        ok, buf = cv2.imencode(".jpg", image)
        if ok:
            with self._lock:
                self._debug_jpeg = buf.tobytes()

    def snapshot(self) -> dict:
        now = self._now()
        with self._lock:
            if self._last_success is None:
                status = "error"
            elif now - self._last_success > timedelta(minutes=self._config.tuning.stale_minutes):
                status = "stale"
            else:
                status = "ok"
            return {
                "total_gal": round(self._last_total, 3) if self._last_total is not None else None,
                "flow_gpm": round(self._flow.flow_gpm(now), 3),
                "continuous_flow_minutes": self._flow.continuous_flow_minutes(now) if self._last_success else 0,
                "status": status,
                "last_success": _iso(self._last_success),
                "last_error": self._last_error,
            }

    def debug_jpeg(self) -> bytes | None:
        with self._lock:
            return self._debug_jpeg
