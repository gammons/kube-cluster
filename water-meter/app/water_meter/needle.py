import math

import cv2
import numpy as np

from water_meter.config import Calibration

HUB_MARGIN = 1.3


def read_needle(meter: np.ndarray, cal: Calibration, min_pixels: int = 20) -> float | None:
    """Return the needle position as a fraction of a revolution from the dial's zero, clockwise."""
    hsv = cv2.cvtColor(meter, cv2.COLOR_BGR2HSV)
    hue, sat, val = hsv[..., 0], hsv[..., 1], hsv[..., 2]
    red = ((hue < 10) | (hue > 170)) & (sat > 60) & (val > 60)

    ys, xs = np.nonzero(red)
    dx = xs - cal.dial_center[0]
    dy = ys - cal.dial_center[1]
    radius = np.hypot(dx, dy)
    keep = (radius > cal.hub_radius * HUB_MARGIN) & (radius <= cal.rim_radius)
    if np.count_nonzero(keep) < min_pixels:
        return None

    angles = np.arctan2(dx[keep], -dy[keep])
    mean = math.degrees(math.atan2(np.sin(angles).sum(), np.cos(angles).sum()))
    fraction = ((mean - cal.zero_angle_deg) % 360.0) / 360.0
    return 0.0 if fraction >= 1.0 else fraction
