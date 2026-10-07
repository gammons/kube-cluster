import cv2
import numpy as np

from water_meter.config import Calibration


def deskew(frame: np.ndarray, cal: Calibration) -> np.ndarray:
    """Rotate the frame about the meter crop centre, then return the meter crop."""
    x, y, w, h = cal.meter_crop
    centre = (x + w / 2.0, y + h / 2.0)
    matrix = cv2.getRotationMatrix2D(centre, cal.rotation_deg, 1.0)
    matrix[0, 2] -= x
    matrix[1, 2] -= y
    return cv2.warpAffine(frame, matrix, (w, h), flags=cv2.INTER_LINEAR, borderMode=cv2.BORDER_REPLICATE)


def digit_crops(meter: np.ndarray, cal: Calibration, dx: int = 0, dy: int = 0) -> list[np.ndarray]:
    return [meter[y + dy : y + dy + h, x + dx : x + dx + w].copy() for x, y, w, h in cal.digit_boxes]
