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
    storage: dict[str, Any] = field(default_factory=dict)
    server: ServerConfig = field(default_factory=ServerConfig)
    logging: LoggingConfig = field(default_factory=LoggingConfig)


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
        storage=raw.get("storage", {}),
        server=ServerConfig(**raw.get("server", {})),
        logging=LoggingConfig(**raw.get("logging", {})),
    )
