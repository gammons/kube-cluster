import logging
from datetime import datetime, timedelta

from water_meter.config import Tuning

log = logging.getLogger(__name__)


class InconsistentReading(ValueError):
    pass


def _dial_distance(a: float, b: float) -> float:
    d = abs(a - b) % 10.0
    return min(d, 10.0 - d)


def resolve_reading(wheels: list[float], needle: float, tuning: Tuning) -> float:
    """Combine raw wheel values (0-10, left to right) and the needle fraction into gallons.

    The last wheel turns continuously with the needle; the wheels to its left only
    advance while their right neighbour passes from 9 to 0.
    """
    last_wheel = needle * 10.0
    if _dial_distance(wheels[-1], last_wheel) > tuning.consistency_tolerance * 10.0:
        raise InconsistentReading(f"last wheel {wheels[-1]:.1f} disagrees with needle {needle:.3f}")

    right = last_wheel
    digits: list[int] = []
    for raw in reversed(wheels[:-1]):
        advance = max(0.0, right - 9.0)
        digit = min(range(10), key=lambda n: _dial_distance(raw, n + advance))
        digits.insert(0, digit)
        right = digit + advance

    whole = int("".join(str(d) for d in digits))
    return (whole + needle) * tuning.needle_units_per_rev


class Plausibility:
    def __init__(self, tuning: Tuning, last_total: float | None = None, last_ts: datetime | None = None):
        self._tuning = tuning
        self.last_total = last_total
        self.last_ts = last_ts
        self.last_rejection: str | None = None
        self._rejected: list[tuple[float, datetime]] = []

    def _accept(self, total: float, ts: datetime) -> float:
        self.last_total = total
        self.last_ts = ts
        self.last_rejection = None
        self._rejected.clear()
        return total

    def check(self, total: float, ts: datetime) -> float | None:
        t = self._tuning
        if self.last_total is None or self.last_ts is None:
            return self._accept(total, ts)

        delta = total - self.last_total
        elapsed_min = max(0.0, (ts - self.last_ts).total_seconds() / 60.0)
        if -t.jitter_gal <= delta < 0:
            return self._accept(self.last_total, ts)
        if delta < 0:
            reason = f"went backwards by {-delta:.3f} gal"
        elif delta > t.max_gpm * elapsed_min + t.jitter_gal:
            reason = f"jumped {delta:.3f} gal in {elapsed_min:.2f} min"
        else:
            return self._accept(total, ts)

        self.last_rejection = reason
        self._rejected.append((total, ts))
        while max(v for v, _ in self._rejected) - min(v for v, _ in self._rejected) > t.recovery_agree_gal:
            self._rejected.pop(0)
        waited = ts - self._rejected[0][1]
        if waited >= timedelta(minutes=t.recovery_minutes):
            log.warning("accepting %.3f gal after %s of consistent rejections (was %.3f)", total, waited, self.last_total)
            return self._accept(total, ts)
        return None
