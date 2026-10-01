import os

import cv2
import numpy as np

os.environ.setdefault("OPENCV_FFMPEG_CAPTURE_OPTIONS", "rtsp_transport;tcp")


class CaptureError(RuntimeError):
    pass


def grab_frame(rtsp_url: str, discard: int = 5, timeout_s: float = 10.0) -> np.ndarray:
    """Open the stream, skip a few frames (the first can be partial), return the next one."""
    timeout_ms = int(timeout_s * 1000)
    cap = cv2.VideoCapture(
        rtsp_url,
        cv2.CAP_FFMPEG,
        [cv2.CAP_PROP_OPEN_TIMEOUT_MSEC, timeout_ms, cv2.CAP_PROP_READ_TIMEOUT_MSEC, timeout_ms],
    )
    try:
        if not cap.isOpened():
            raise CaptureError("cannot open stream")
        for _ in range(discard):
            if not cap.grab():
                raise CaptureError("stream ended while skipping frames")
        ok, frame = cap.read()
        if not ok or frame is None:
            raise CaptureError("no frame received")
        return frame
    finally:
        cap.release()
