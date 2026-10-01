"""Render calibration overlays: python -m water_meter.calibrate <frame.jpg> <config.yaml> <out_dir>"""

import sys
from pathlib import Path

import cv2

from water_meter.config import load_config
from water_meter.debug import render
from water_meter.regions import deskew, digit_crops


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        print(__doc__, file=sys.stderr)
        return 2
    frame_path, config_path, out_dir = (Path(a) for a in argv)
    frame = cv2.imread(str(frame_path))
    if frame is None:
        print(f"cannot read {frame_path}", file=sys.stderr)
        return 1
    cal = load_config(config_path).calibration
    out_dir.mkdir(parents=True, exist_ok=True)

    meter = deskew(frame, cal)
    overlay = cv2.resize(render(meter, cal), None, fx=3, fy=3, interpolation=cv2.INTER_NEAREST)
    cv2.imwrite(str(out_dir / "overlay.png"), overlay)
    for i, crop in enumerate(digit_crops(meter, cal)):
        cv2.imwrite(str(out_dir / f"digit_{i}.png"), cv2.resize(crop, None, fx=4, fy=4, interpolation=cv2.INTER_NEAREST))
    print(f"wrote {out_dir}/overlay.png and digit_0..7.png")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
