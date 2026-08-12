"""Hardened webhook delivery: HMAC body signing, idempotency, exponential backoff, DLQ."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
from pathlib import Path
from typing import Any

import aiofiles
import aiohttp
import structlog

log = structlog.get_logger(__name__)


class WebhookDelivery:
    def __init__(
        self,
        *,
        endpoints: list[Any],
        max_attempts: int = 5,
        initial_backoff_seconds: float = 1.0,
        max_backoff_seconds: float = 60.0,
        dead_letter_path: str | Path = "./dlq",
    ) -> None:
        self.endpoints = endpoints
        self.max_attempts = max_attempts
        self.initial_backoff = initial_backoff_seconds
        self.max_backoff = max_backoff_seconds
        self.dlq = Path(dead_letter_path)
        self.dlq.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def _sign(body: bytes, secret: str) -> str:
        mac = hmac.new(secret.encode("utf-8"), body, hashlib.sha256)
        return "sha256=" + mac.hexdigest()

    async def deliver(self, vcon_dict: dict[str, Any]) -> bool:
        """Deliver to all endpoints. Returns True if at least one succeeded."""
        body = json.dumps(vcon_dict, separators=(",", ":")).encode("utf-8")
        uuid = vcon_dict["uuid"]
        any_success = False

        async with aiohttp.ClientSession() as session:
            for ep in self.endpoints:
                ok = await self._deliver_one(session, ep, body, uuid)
                any_success = any_success or ok

        if not any_success:
            await self._write_dlq(uuid, body)
        return any_success

    async def _deliver_one(
        self,
        session: aiohttp.ClientSession,
        endpoint: Any,
        body: bytes,
        uuid: str,
    ) -> bool:
        headers = {
            "Content-Type": "application/json",
            "Idempotency-Key": uuid,
        }
        if endpoint.hmac_secret:
            headers["X-Hub-Signature-256"] = self._sign(body, endpoint.hmac_secret)

        backoff = self.initial_backoff
        for attempt in range(1, self.max_attempts + 1):
            try:
                async with session.post(
                    endpoint.url,
                    data=body,
                    headers=headers,
                    timeout=aiohttp.ClientTimeout(total=endpoint.timeout_seconds),
                ) as resp:
                    if 200 <= resp.status < 300:
                        log.info("delivered", url=endpoint.url, uuid=uuid, attempt=attempt)
                        return True
                    log.warning(
                        "delivery_failed",
                        url=endpoint.url,
                        status=resp.status,
                        attempt=attempt,
                    )
            except Exception as e:
                log.warning("delivery_error", url=endpoint.url, error=str(e), attempt=attempt)

            if attempt < self.max_attempts:
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, self.max_backoff)

        return False

    async def _write_dlq(self, uuid: str, body: bytes) -> None:
        path = self.dlq / f"{uuid}.vcon.json"
        async with aiofiles.open(path, "wb") as f:
            await f.write(body)
        log.error("dlq_write", uuid=uuid, path=str(path))
