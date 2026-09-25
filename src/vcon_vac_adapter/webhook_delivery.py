"""Hardened delivery: webhook (HMAC-signed) and conserver-direct, sharing one
retry/backoff/dead-letter-queue implementation.

- `WebhookDelivery`: generic webhook, HMAC `X-Hub-Signature-256`,
  `Idempotency-Key`, backoff, dead-letter queue. Fans out to N endpoints.
- `ConserverDelivery`: `POST {base_url}/vcon` directly against a vcon-server
  instance, with a bearer-style API-key header and optional ingress-list
  query params. Reuses the same retry/backoff/DLQ logic.

Pick between them via `delivery.mode: webhook|conserver` in config (see
`config.py`'s `DeliveryConfig`).

Both `deliver()` methods call `vcon_builder.finalize_vcon()` on the incoming
`vcon_dict` before serializing it, so callers get vcon-lib's empty
`meta`/`metadata` placeholders stripped for free.

`ConserverDelivery` and the shared `_post_with_retry` / `_write_dlq` helpers
below are copied from `vcon-dev/vcon-adapter-template`'s
`src/__ADAPTER_PACKAGE__/webhook_delivery.py` (`vcon-dev/vcon-adapter-template`,
pull request #1) — this module already shared that template's HMAC/retry/
backoff/DLQ shape for `WebhookDelivery` before this change, so the retry
loop was pulled out into a function both classes call rather than
duplicated.
"""

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

from .vcon_builder import finalize_vcon

log = structlog.get_logger(__name__)


async def _post_with_retry(
    session: aiohttp.ClientSession,
    *,
    url: str,
    headers: dict[str, str],
    body: bytes,
    params: Any = None,
    timeout_seconds: int,
    max_attempts: int,
    initial_backoff: float,
    max_backoff: float,
    uuid: str,
) -> bool:
    """POST `body` to `url` with exponential-backoff retries. Shared by
    WebhookDelivery (per endpoint) and ConserverDelivery (single target).
    """
    backoff = initial_backoff
    for attempt in range(1, max_attempts + 1):
        try:
            async with session.post(
                url,
                data=body,
                headers=headers,
                params=params,
                timeout=aiohttp.ClientTimeout(total=timeout_seconds),
            ) as resp:
                if 200 <= resp.status < 300:
                    log.info("delivered", url=url, uuid=uuid, attempt=attempt)
                    return True
                log.warning(
                    "delivery_failed",
                    url=url,
                    status=resp.status,
                    attempt=attempt,
                )
        except Exception as e:
            log.warning("delivery_error", url=url, error=str(e), attempt=attempt)

        if attempt < max_attempts:
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, max_backoff)

    return False


async def _write_dlq(dlq: Path, uuid: str, body: bytes) -> None:
    path = dlq / f"{uuid}.vcon.json"
    async with aiofiles.open(path, "wb") as f:
        await f.write(body)
    log.error("dlq_write", uuid=uuid, path=str(path))


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
        finalize_vcon(vcon_dict)
        body = json.dumps(vcon_dict, separators=(",", ":")).encode("utf-8")
        uuid = vcon_dict["uuid"]
        any_success = False

        async with aiohttp.ClientSession() as session:
            for ep in self.endpoints:
                ok = await self._deliver_one(session, ep, body, uuid)
                any_success = any_success or ok

        if not any_success:
            await _write_dlq(self.dlq, uuid, body)
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

        return await _post_with_retry(
            session,
            url=endpoint.url,
            headers=headers,
            body=body,
            timeout_seconds=endpoint.timeout_seconds,
            max_attempts=self.max_attempts,
            initial_backoff=self.initial_backoff,
            max_backoff=self.max_backoff,
            uuid=uuid,
        )


class ConserverDelivery:
    """POST vCons directly to a vcon-server `/vcon` endpoint.

    Query param is `ingress_lists` (plural, repeatable) — matches the
    `POST /vcon` route in vcon-server's `api/api.py`
    (`ingress_lists: Optional[List[str]] = Query(None, ...)`), not the
    singular `ingress_list` used by the separate `/vcon/external-ingress`
    and `/vcon/ingress` routes.

    Auth header defaults to `x-conserver-api-token` (vcon-server's
    `CONSERVER_HEADER_NAME` default, `common/settings.py`), configurable via
    `token_header` for a non-default deployment.
    """

    def __init__(
        self,
        *,
        base_url: str,
        token: str = "",
        token_header: str = "x-conserver-api-token",
        ingress_lists: list[str] | None = None,
        timeout_seconds: int = 30,
        max_attempts: int = 5,
        initial_backoff_seconds: float = 1.0,
        max_backoff_seconds: float = 60.0,
        dead_letter_path: str | Path = "./dlq",
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.token_header = token_header
        self.ingress_lists = list(ingress_lists or [])
        self.timeout_seconds = timeout_seconds
        self.max_attempts = max_attempts
        self.initial_backoff = initial_backoff_seconds
        self.max_backoff = max_backoff_seconds
        self.dlq = Path(dead_letter_path)
        self.dlq.mkdir(parents=True, exist_ok=True)

    async def deliver(self, vcon_dict: dict[str, Any]) -> bool:
        """POST to `{base_url}/vcon`. Returns True on success."""
        finalize_vcon(vcon_dict)
        body = json.dumps(vcon_dict, separators=(",", ":")).encode("utf-8")
        uuid = vcon_dict["uuid"]

        headers = {
            "Content-Type": "application/json",
            "Idempotency-Key": uuid,
        }
        if self.token:
            headers[self.token_header] = self.token

        params = [("ingress_lists", name) for name in self.ingress_lists] or None

        async with aiohttp.ClientSession() as session:
            ok = await _post_with_retry(
                session,
                url=f"{self.base_url}/vcon",
                headers=headers,
                body=body,
                params=params,
                timeout_seconds=self.timeout_seconds,
                max_attempts=self.max_attempts,
                initial_backoff=self.initial_backoff,
                max_backoff=self.max_backoff,
                uuid=uuid,
            )

        if not ok:
            await _write_dlq(self.dlq, uuid, body)
        return ok
