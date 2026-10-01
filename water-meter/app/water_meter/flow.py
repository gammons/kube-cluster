from collections import deque
from datetime import datetime, timedelta

from water_meter.config import Tuning


class FlowTracker:
    def __init__(self, tuning: Tuning, streak_start: datetime | None = None):
        self._tuning = tuning
        self.streak_start = streak_start
        self._samples: deque[tuple[datetime, float]] = deque()

    def add(self, total: float, ts: datetime) -> None:
        t = self._tuning
        if self.streak_start is None:
            self.streak_start = ts
        self._samples.append((ts, total))

        window = timedelta(minutes=t.still_window_minutes)
        keep_from = ts - window - timedelta(minutes=1)
        while self._samples and self._samples[0][0] < keep_from:
            self._samples.popleft()

        window_start = ts - window
        anchor = None
        for i, (sample_ts, _) in enumerate(self._samples):
            if sample_ts <= window_start:
                anchor = i
        if anchor is None:
            return
        totals = [v for _, v in list(self._samples)[anchor:]]
        if max(totals) - min(totals) < t.still_threshold_gal:
            self.streak_start = ts

    def continuous_flow_minutes(self, now: datetime) -> int:
        if self.streak_start is None:
            return 0
        return max(0, int((now - self.streak_start).total_seconds() // 60))

    def flow_gpm(self, now: datetime) -> float:
        since = now - timedelta(minutes=self._tuning.flow_window_minutes)
        recent = [(ts, v) for ts, v in self._samples if ts >= since]
        if len(recent) < 2:
            return 0.0
        minutes = (recent[-1][0] - recent[0][0]).total_seconds() / 60.0
        if minutes < 1.0:
            return 0.0
        return (recent[-1][1] - recent[0][1]) / minutes
