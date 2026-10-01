import math

import cv2
import numpy as np

from water_meter.config import Calibration

GREEN = (0, 200, 0)
BLUE = (255, 120, 0)
MAGENTA = (255, 0, 255)


def _point_at(cal: Calibration, angle_deg: float, radius: float) -> tuple[int, int]:
    cx, cy = cal.dial_center
    a = math.radians(angle_deg)
    return (int(round(cx + radius * math.sin(a))), int(round(cy - radius * math.cos(a))))


def render(
    meter: np.ndarray,
    cal: Calibration,
    digit_values: list[float] | None = None,
    needle: float | None = None,
    total: float | None = None,
) -> np.ndarray:
    out = meter.copy()
    for i, (x, y, w, h) in enumerate(cal.digit_boxes):
        cv2.rectangle(out, (x, y), (x + w - 1, y + h - 1), GREEN, 1)
        if digit_values is not None:
            cv2.putText(out, f"{digit_values[i]:.1f}", (x, max(10, y - 4)), cv2.FONT_HERSHEY_PLAIN, 0.8, GREEN, 1)

    centre = (int(round(cal.dial_center[0])), int(round(cal.dial_center[1])))
    cv2.circle(out, centre, int(round(cal.hub_radius)), BLUE, 1)
    cv2.circle(out, centre, int(round(cal.rim_radius)), BLUE, 1)
    cv2.line(
        out,
        _point_at(cal, cal.zero_angle_deg, cal.rim_radius - 8),
        _point_at(cal, cal.zero_angle_deg, cal.rim_radius + 8),
        BLUE,
        2,
    )
    if needle is not None:
        angle = cal.zero_angle_deg + needle * 360.0
        cv2.line(out, centre, _point_at(cal, angle, cal.rim_radius), MAGENTA, 1)

    if total is not None:
        cv2.putText(out, f"{total:.3f} gal", (5, out.shape[0] - 8), cv2.FONT_HERSHEY_SIMPLEX, 0.6, MAGENTA, 2)
    return out
