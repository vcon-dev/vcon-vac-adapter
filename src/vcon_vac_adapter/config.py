"""YAML config loader with ${ENV_VAR} substitution."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

_ENV_RE = re.compile(r"\$\{([A-Z0-9_]+)\}")


def _substitute(value: Any) -> Any:
    if isinstance(value, str):
        return _ENV_RE.sub(lambda m: os.environ.get(m.group(1), ""), value)
    if isinstance(value, dict):
        return {k: _substitute(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_substitute(v) for v in value]
    return value


@dataclass
class AdapterConfig:
    name: str = "unnamed"
    source_platform: str = ""


@dataclass
class WebhookEndpoint:
    url: str
    hmac_secret: str = ""
    timeout_seconds: int = 30


@dataclass
class WebhookConfig:
    endpoints: list[WebhookEndpoint] = field(default_factory=list)
    retry_max_attempts: int = 5
    retry_initial_backoff_seconds: float = 1.0
    retry_max_backoff_seconds: float = 60.0
    dead_letter_path: str = "./dlq"


@dataclass
class ConserverConfig:
    """Direct-to-conserver delivery: `POST {base_url}/vcon`. See
    `webhook_delivery.ConserverDelivery`.
    """

    base_url: str = ""
    token: str = ""
    token_header: str = "x-conserver-api-token"
    ingress_lists: list[str] = field(default_factory=list)
    timeout_seconds: int = 30


@dataclass
class DeliveryConfig:
    """Picks between `webhook` and `conserver` delivery (both share the
    `webhook.retry` / `webhook.dead_letter_path` settings).
    """

    mode: str = "webhook"  # "webhook" | "conserver"

    def __post_init__(self) -> None:
        if self.mode not in ("webhook", "conserver"):
            raise ValueError(f"delivery.mode must be 'webhook' or 'conserver', got {self.mode!r}")


@dataclass
class ServerConfig:
    host: str = "0.0.0.0"
    port: int = 8000


@dataclass
class LoggingConfig:
    level: str = "INFO"
    format: str = "json"


@dataclass
class Config:
    adapter: AdapterConfig = field(default_factory=AdapterConfig)
    source: dict[str, Any] = field(default_factory=dict)
    vcon: dict[str, Any] = field(default_factory=dict)
    webhook: WebhookConfig = field(default_factory=WebhookConfig)
    conserver: ConserverConfig = field(default_factory=ConserverConfig)
    delivery: DeliveryConfig = field(default_factory=DeliveryConfig)
    storage: dict[str, Any] = field(default_factory=dict)
    server: ServerConfig = field(default_factory=ServerConfig)
    logging: LoggingConfig = field(default_factory=LoggingConfig)

    def watch_dir(self) -> Path | None:
        """The configured Claude Code session directory to watch in daemon
        mode, or `None` if unset. NEVER defaults to the real
        `~/.claude/projects` — an adapter operator must set
        `source.claude_code.watch_dir` explicitly (typically via
        `${SOME_ENV_VAR}` substitution) for daemon mode to watch anything.
        """
        raw = self.source.get("claude_code", {}).get("watch_dir")
        return Path(raw) if raw else None


def load_config(path: str | Path = "config.yaml") -> Config:
    raw = yaml.safe_load(Path(path).read_text())
    raw = _substitute(raw)

    webhook_raw = raw.get("webhook", {})
    endpoints = [WebhookEndpoint(**e) for e in webhook_raw.get("endpoints", [])]
    retry = webhook_raw.get("retry", {})

    return Config(
        adapter=AdapterConfig(**raw.get("adapter", {})),
        source=raw.get("source", {}),
        vcon=raw.get("vcon", {}),
        webhook=WebhookConfig(
            endpoints=endpoints,
            retry_max_attempts=retry.get("max_attempts", 5),
            retry_initial_backoff_seconds=retry.get("initial_backoff_seconds", 1.0),
            retry_max_backoff_seconds=retry.get("max_backoff_seconds", 60.0),
            dead_letter_path=webhook_raw.get("dead_letter_path", "./dlq"),
        ),
        conserver=ConserverConfig(**raw.get("conserver", {})),
        delivery=DeliveryConfig(**raw.get("delivery", {})),
        storage=raw.get("storage", {}),
        server=ServerConfig(**raw.get("server", {})),
        logging=LoggingConfig(**raw.get("logging", {})),
    )
