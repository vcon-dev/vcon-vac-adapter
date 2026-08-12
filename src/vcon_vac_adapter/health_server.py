"""HTTP server exposing /healthz and Prometheus /metrics."""

from __future__ import annotations

from aiohttp import web
from prometheus_client import CONTENT_TYPE_LATEST, Counter, generate_latest

vcons_built = Counter("vcons_built_total", "vCons constructed")
vcons_delivered = Counter("vcons_delivered_total", "vCons successfully delivered", ["endpoint"])
vcons_dlq = Counter("vcons_dlq_total", "vCons written to dead-letter queue")


class HealthServer:
    def __init__(self, *, host: str = "0.0.0.0", port: int = 8000) -> None:
        self.host = host
        self.port = port
        self._runner: web.AppRunner | None = None

    async def start(self) -> None:
        app = web.Application()
        app.router.add_get("/healthz", self._healthz)
        app.router.add_get("/metrics", self._metrics)
        self._runner = web.AppRunner(app)
        await self._runner.setup()
        site = web.TCPSite(self._runner, self.host, self.port)
        await site.start()

    async def stop(self) -> None:
        if self._runner:
            await self._runner.cleanup()

    @staticmethod
    async def _healthz(_: web.Request) -> web.Response:
        return web.json_response({"status": "ok"})

    @staticmethod
    async def _metrics(_: web.Request) -> web.Response:
        return web.Response(body=generate_latest(), content_type=CONTENT_TYPE_LATEST)
