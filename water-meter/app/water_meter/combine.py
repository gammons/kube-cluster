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

    One needle revolution moves the last wheel by one digit, so the last wheel's
    position is ``digit + needle``. The wheels to its left only advance while their
    right neighbour passes from 9 to 0.
    """
    last_digit = min(range(10), key=lambda n: _dial_distance(wheels[-1], n + needle))
    right = last_digit + needle
    if _dial_distance(wheels[-1], right) > tuning.consistency_tolerance:
        raise InconsistentReading(f"last wheel {wheels[-1]:.1f} disagrees with needle {needle:.3f}")

    digits = [last_digit]
    for raw in reversed(wheels[:-1]):
        advance = max(0.0, right - 9.0)
        digit = min(range(10), key=lambda n: _dial_distance(raw, n + advance))
        digits.insert(0, digit)
        right = digit + advance

    whole = int("".join(str(d) for d in digits))
    return whole * tuning.wheel_units + needle * tuning.needle_units_per_rev


class Plausibility:
    """Accepts a reading only if it is physically consistent with the last accepted one.

    Jumps above ``confirm_jump_gal`` (and the very first reading) must be confirmed by
    ``confirm_samples`` consecutive consistent readings. Readings below the stored value
    that stay rejected but consistent with each other for ``recovery_minutes`` replace it.
    Readings above it are never accepted that way: a jump faster than ``max_gpm`` when it
    first appears is a misread, however long it persists.
    """

    def __init__(self, tuning: Tuning, last_total: float | None = None, last_ts: datetime | None = None):
        self._tuning = tuning
        self.last_total = last_total
        self.last_ts = last_ts
        self.last_rejection: str | None = None
        self._pending: list[tuple[float, datetime]] = []
        self._rejected: list[tuple[float, datetime]] = []

    def _consistent(self, earlier: tuple[float, datetime], total: float, ts: datetime) -> bool:
        t = self._tuning
        value, then = earlier
        elapsed_min = max(0.0, (ts - then).total_seconds() / 60.0)
        delta = total - value
        return -t.jitter_gal <= delta <= t.max_gpm * elapsed_min + t.jitter_gal

    def _extend(self, chain: list[tuple[float, datetime]], total: float, ts: datetime) -> None:
        if chain and not self._consistent(chain[-1], total, ts):
            chain.clear()
        chain.append((total, ts))

    def _continues_rejected_jump(self, total: float, ts: datetime) -> bool:
        # A jump that was faster than max_gpm when it first appeared stays impossible,
        # however long it persists.
        return (
            bool(self._rejected)
            and self._rejected[0][0] > self.last_total
            and self._consistent(self._rejected[-1], total, ts)
        )

    def _accept(self, total: float, ts: datetime) -> float:
        self.last_total = total
        self.last_ts = ts
        self.last_rejection = None
        self._pending.clear()
        self._rejected.clear()
        return total

    def _confirm(self, total: float, ts: datetime, reason: str) -> float | None:
        self._rejected.clear()
        self._extend(self._pending, total, ts)
        if len(self._pending) >= self._tuning.confirm_samples:
            return self._accept(total, ts)
        self.last_rejection = reason
        return None

    def check(self, total: float, ts: datetime) -> float | None:
        t = self._tuning
        if self.last_total is None or self.last_ts is None:
            return self._confirm(total, ts, "awaiting confirmation of first reading")

        delta = total - self.last_total
        if -t.jitter_gal <= delta < 0:
            return self._accept(self.last_total, ts)
        if not self._continues_rejected_jump(total, ts) and self._consistent((self.last_total, self.last_ts), total, ts):
            if delta <= t.confirm_jump_gal:
                return self._accept(total, ts)
            return self._confirm(total, ts, f"awaiting confirmation of {delta:.3f} gal jump")

        self._pending.clear()
        self.last_rejection = f"went backwards by {-delta:.3f} gal" if delta < 0 else f"jumped {delta:.3f} gal"
        self._extend(self._rejected, total, ts)
        waited = ts - self._rejected[0][1]
        if delta < 0 and waited >= timedelta(minutes=t.recovery_minutes):
            log.warning("accepting %.3f gal after %s of consistent rejections (was %.3f)", total, waited, self.last_total)
            return self._accept(total, ts)
        return None
