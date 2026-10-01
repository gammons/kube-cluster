import logging
import os
import sys
from functools import partial
from pathlib import Path

import uvicorn

from water_meter.api import create_app
from water_meter.capture import grab_frame
from water_meter.config import load_config
from water_meter.digits import DigitReader
from water_meter.service import MeterService


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    rtsp_url = os.environ.get("RTSP_URL")
    if not rtsp_url:
        print("RTSP_URL is required", file=sys.stderr)
        return 2
    config = load_config(Path(os.environ.get("CONFIG_PATH", "/config/config.yaml")))
    reader = DigitReader(Path(os.environ.get("MODEL_PATH", "/app/models/dig-class100-0182-s2_q.tflite")))
    service = MeterService(
        config,
        reader,
        partial(grab_frame, rtsp_url),
        Path(os.environ.get("STATE_PATH", "/state/state.json")),
    )
    app = create_app(service, interval_s=config.tuning.sample_interval_s)
    uvicorn.run(app, host="0.0.0.0", port=8080, log_level="info")
    return 0


if __name__ == "__main__":
    sys.exit(main())
