import logging
import threading
from contextlib import asynccontextmanager

from fastapi import FastAPI, Response

from water_meter.service import MeterService

log = logging.getLogger(__name__)


def _loop(service: MeterService, interval_s: float, stop: threading.Event) -> None:
    while not stop.is_set():
        service.sample()
        stop.wait(interval_s)


def create_app(service: MeterService, run_loop: bool = True, interval_s: float = 15.0) -> FastAPI:
    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        stop = threading.Event()
        worker = None
        if run_loop:
            worker = threading.Thread(target=_loop, args=(service, interval_s, stop), name="sampler", daemon=True)
            worker.start()
        yield
        stop.set()
        if worker is not None:
            worker.join(timeout=interval_s + 15)

    app = FastAPI(title="water-meter", lifespan=lifespan)

    @app.get("/reading")
    def reading() -> dict:
        return service.snapshot()

    @app.get("/debug.jpg")
    def debug() -> Response:
        image = service.debug_jpeg()
        if image is None:
            return Response(status_code=404)
        return Response(content=image, media_type="image/jpeg")

    @app.get("/healthz")
    def healthz() -> dict:
        return {"ok": True}

    return app
