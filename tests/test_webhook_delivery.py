"""Webhook and conserver-direct delivery tests: HMAC signature shape/
verification against a real mock server, retry, DLQ, finalize_vcon
integration.
"""

from __future__ import annotations

import hashlib
import hmac
import json

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from vcon_vac_adapter.webhook_delivery import ConserverDelivery, WebhookDelivery


class _Endpoint:
    def __init__(self, url: str, secret: str = "", timeout: int = 30) -> None:
        self.url = url
        self.hmac_secret = secret
        self.timeout_seconds = timeout


def test_hmac_signature_format() -> None:
    body = b'{"vcon":"0.4.0"}'
    secret = "shh"
    sig = WebhookDelivery._sign(body, secret)
    assert sig.startswith("sha256=")
    expected = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    assert sig == f"sha256={expected}"


@pytest.mark.asyncio
async def test_dlq_written_when_no_endpoints(tmp_path) -> None:
    wd = WebhookDelivery(endpoints=[], dead_letter_path=tmp_path)
    vcon = {"uuid": "abc-123", "vcon": "0.4.0"}
    ok = await wd.deliver(vcon)
    assert ok is False
    dlq_file = tmp_path / "abc-123.vcon.json"
    assert dlq_file.exists()
    assert json.loads(dlq_file.read_text())["uuid"] == "abc-123"


@pytest.mark.asyncio
async def test_webhook_delivery_hmac_verified_against_mock_endpoint(tmp_path) -> None:
    """Runs a real aiohttp server, delivers to it, and independently
    recomputes the expected HMAC from the received body to confirm
    WebhookDelivery signs exactly what it sends.
    """
    secret = "top-secret"
    received: dict[str, object] = {}

    async def handler(request: web.Request) -> web.Response:
        received["body"] = await request.read()
        received["signature"] = request.headers.get("X-Hub-Signature-256")
        received["idempotency_key"] = request.headers.get("Idempotency-Key")
        return web.json_response({"ok": True})

    app = web.Application()
    app.router.add_post("/webhook", handler)
    server = TestServer(app)
    await server.start_server()
    try:
        endpoint = _Endpoint(str(server.make_url("/webhook")), secret=secret)
        wd = WebhookDelivery(endpoints=[endpoint], dead_letter_path=tmp_path)
        vcon = {"uuid": "hmac-test-1", "vcon": "0.4.0", "dialog": [{"meta": {}}]}

        ok = await wd.deliver(vcon)

        assert ok is True
        assert received["idempotency_key"] == "hmac-test-1"
        body = received["body"]
        assert isinstance(body, bytes)
        # finalize_vcon must have stripped the empty dialog `meta` before signing.
        assert json.loads(body)["dialog"] == [{}]
        expected_sig = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
        assert received["signature"] == expected_sig
    finally:
        await server.close()


@pytest.mark.asyncio
async def test_conserver_delivery_posts_to_vcon_endpoint_with_ingress_lists(tmp_path) -> None:
    received: dict[str, object] = {}

    async def handler(request: web.Request) -> web.Response:
        received["path"] = request.path
        received["query"] = request.query.getall("ingress_lists")
        received["token_header"] = request.headers.get("x-conserver-api-token")
        received["body"] = await request.read()
        return web.json_response({"ok": True})

    app = web.Application()
    app.router.add_post("/vcon", handler)
    server = TestServer(app)
    await server.start_server()
    try:
        base_url = str(server.make_url("")).rstrip("/")
        cd = ConserverDelivery(
            base_url=base_url,
            token="conserver-token",
            ingress_lists=["default", "compliance"],
            dead_letter_path=tmp_path,
        )
        vcon = {"uuid": "conserver-test-1", "vcon": "0.4.0"}

        ok = await cd.deliver(vcon)

        assert ok is True
        assert received["path"] == "/vcon"
        assert received["query"] == ["default", "compliance"]
        assert received["token_header"] == "conserver-token"
    finally:
        await server.close()
