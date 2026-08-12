"""Webhook delivery tests: HMAC signature shape, retry, DLQ."""

from __future__ import annotations

import hashlib
import hmac
import json

import pytest

from vcon_vac_adapter.webhook_delivery import WebhookDelivery


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
