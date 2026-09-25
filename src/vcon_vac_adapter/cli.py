"""Entry point.

Two subcommands:
  * `vac-adapter convert <session-file> --platform <p> --out <path> [--post ...]`
      one-shot: parse a saved session file, write the vCon JSON to --out,
      and optionally deliver it (webhook or conserver-direct).
  * `vac-adapter daemon` (or no subcommand)
      long-running: load config, start health/metrics server. If
      `adapter.source_platform` is `claude_code` and `source.claude_code.watch_dir`
      is set, watches that directory for session `.jsonl` files and delivers
      a vCon per new-or-changed session (see `watcher.SessionWatcher`).
      Other platforms have no watcher yet (see README "Daemon" section) and
      the daemon just serves health/metrics until told to stop.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import signal
import sys
from pathlib import Path
from typing import Literal

import structlog

from .config import Config, load_config
from .health_server import HealthServer
from .ir import Session
from .session_vcon import build_vcon
from .sources import claude_code as src_claude_code
from .vcon_builder import LawfulBasisConfig, finalize_vcon
from .watcher import SessionWatcher
from .webhook_delivery import ConserverDelivery, WebhookDelivery

log = structlog.get_logger(__name__)

Granularity = Literal["session", "per_tool_call"]
VacEncoding = Literal["json", "cbor"]


def _parse_one(path: Path, platform: str) -> Session:
    if platform == "claude_code":
        return src_claude_code.parse_file(path)
    if platform == "anthropic":
        from .sources.anthropic import parse_trace

        data = json.loads(path.read_text())
        return parse_trace(data.get("request", {}), data.get("response", {}))
    if platform == "openai_responses":
        from .sources.openai_responses import parse_trace

        data = json.loads(path.read_text())
        return parse_trace(data.get("request", {}), data.get("response", {}))
    if platform == "openai_agents":
        from .sources.openai_agents import parse_spans

        spans = json.loads(path.read_text())
        return parse_spans(spans)
    if platform == "otel":
        from .sources.otel import parse_spans as parse_otel

        return parse_otel(json.loads(path.read_text()))
    raise ValueError(f"unknown platform: {platform!r}")


def _delivery_from_args(
    args: argparse.Namespace,
) -> ConserverDelivery | WebhookDelivery | None:
    """Build a WebhookDelivery/ConserverDelivery from one-shot `convert --post`
    flags, without requiring a full config.yaml. Returns None if `--post`
    wasn't given.
    """
    if not args.post:
        return None
    if args.conserver_url:
        return ConserverDelivery(
            base_url=args.conserver_url,
            token=args.conserver_token or "",
            ingress_lists=list(args.ingress_list or []),
        )
    if args.webhook_url:
        endpoint = _SimpleEndpoint(
            url=args.webhook_url, hmac_secret=args.webhook_secret or "", timeout_seconds=30
        )
        return WebhookDelivery(endpoints=[endpoint])
    raise SystemExit("--post requires --conserver-url or --webhook-url")


class _SimpleEndpoint:
    def __init__(self, *, url: str, hmac_secret: str, timeout_seconds: int) -> None:
        self.url = url
        self.hmac_secret = hmac_secret
        self.timeout_seconds = timeout_seconds


def _cmd_convert(args: argparse.Namespace) -> int:
    session = _parse_one(Path(args.input), args.platform)
    v = build_vcon(
        session,
        granularity=args.granularity,
        include_lawful_basis=not args.no_lawful_basis,
        lawful_basis_cfg=LawfulBasisConfig.from_env(),
        vac_encoding=args.vac_encoding,
        critical_agent_session=args.critical_agent_session,
    )
    if args.sign_key:
        v.sign(Path(args.sign_key).read_bytes())
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(v.dumps())
    log.info("wrote", path=str(out), bytes=out.stat().st_size)

    delivery = _delivery_from_args(args)
    if delivery is not None:
        ok = asyncio.run(delivery.deliver(finalize_vcon(v.vcon_dict)))
        log.info("posted", delivered=ok)
        if not ok:
            return 1
    return 0


def _build_delivery(config: Config) -> ConserverDelivery | WebhookDelivery:
    """Build the configured delivery backend (`delivery.mode`) for daemon
    mode. Shared retry/backoff/DLQ settings live under `webhook.retry` and
    `webhook.dead_letter_path` regardless of mode.
    """
    if config.delivery.mode == "conserver":
        return ConserverDelivery(
            base_url=config.conserver.base_url,
            token=config.conserver.token,
            token_header=config.conserver.token_header,
            ingress_lists=config.conserver.ingress_lists,
            timeout_seconds=config.conserver.timeout_seconds,
            max_attempts=config.webhook.retry_max_attempts,
            initial_backoff_seconds=config.webhook.retry_initial_backoff_seconds,
            max_backoff_seconds=config.webhook.retry_max_backoff_seconds,
            dead_letter_path=config.webhook.dead_letter_path,
        )
    return WebhookDelivery(
        endpoints=config.webhook.endpoints,
        max_attempts=config.webhook.retry_max_attempts,
        initial_backoff_seconds=config.webhook.retry_initial_backoff_seconds,
        max_backoff_seconds=config.webhook.retry_max_backoff_seconds,
        dead_letter_path=config.webhook.dead_letter_path,
    )


async def _run_daemon(args: argparse.Namespace) -> int:
    config = load_config()
    logging.basicConfig(level=config.logging.level)
    structlog.configure(processors=[structlog.processors.JSONRenderer()])
    log.info(
        "starting",
        adapter=config.adapter.name,
        version="0.1.0",
        source_platform=config.adapter.source_platform,
    )

    health = HealthServer(host=config.server.host, port=config.server.port)
    await health.start()

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)

    watcher_task: asyncio.Task[None] | None = None
    watch_dir = config.watch_dir()
    if config.adapter.source_platform == "claude_code" and watch_dir is not None:
        delivery = _build_delivery(config)
        watcher = SessionWatcher(
            watch_dir=watch_dir,
            deliver=delivery.deliver,
            granularity=config.vcon.get("granularity", "session"),
            include_lawful_basis=config.vcon.get("include_lawful_basis", True),
            lawful_basis_cfg=LawfulBasisConfig.resolve(yaml_block=config.vcon.get("lawful_basis")),
        )
        log.info("watching", watch_dir=str(watch_dir), mode=config.delivery.mode)
        watcher_task = asyncio.create_task(watcher.run(stop_event=stop))
    else:
        # No watcher configured for this platform/run: source.claude_code.watch_dir
        # is unset, or source_platform isn't claude_code yet. Never falls back to
        # the real ~/.claude/projects — set source.claude_code.watch_dir explicitly.
        log.info(
            "daemon ready with no watcher configured; set adapter.source_platform: "
            "claude_code and source.claude_code.watch_dir to watch a session directory"
        )

    await stop.wait()
    if watcher_task is not None:
        watcher_task.cancel()
        try:
            await watcher_task
        except (asyncio.CancelledError, Exception):
            pass
    await health.stop()
    log.info("stopped")
    return 0


def _cmd_daemon(args: argparse.Namespace) -> int:
    return asyncio.run(_run_daemon(args))


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="vac-adapter", description="Convert AI agent sessions into vCons with VAC records."
    )
    sub = p.add_subparsers(dest="cmd")

    conv = sub.add_parser("convert", help="One-shot: parse a saved session file → vCon JSON.")
    conv.add_argument(
        "input", help="Path to the session file (.jsonl for claude_code, .json for others)"
    )
    conv.add_argument(
        "--platform",
        required=True,
        choices=("claude_code", "anthropic", "openai_responses", "openai_agents", "otel"),
    )
    conv.add_argument("--out", required=True, help="Path to write the .vcon.json file")
    conv.add_argument("--granularity", default="session", choices=("session", "per_tool_call"))
    conv.add_argument("--vac-encoding", default="json", choices=("json", "cbor"))
    conv.add_argument(
        "--critical-agent-session",
        action="store_true",
        help='Mark "agent_session" in vCon critical[]',
    )
    conv.add_argument(
        "--no-lawful-basis", action="store_true", help="Skip emitting the lawful_basis attachment"
    )
    conv.add_argument("--sign-key", help="PEM private key; JWS-sign the vCon before writing")
    conv.add_argument(
        "--post", action="store_true", help="Also deliver the vCon (webhook or conserver-direct)"
    )
    conv.add_argument("--webhook-url", help="--post target: generic webhook URL")
    conv.add_argument("--webhook-secret", help="--post: HMAC secret for --webhook-url")
    conv.add_argument("--conserver-url", help="--post target: vcon-server base URL")
    conv.add_argument("--conserver-token", help="--post: vcon-server API token")
    conv.add_argument(
        "--ingress-list",
        action="append",
        help="--post with --conserver-url: ingress_lists query param (repeatable)",
    )

    sub.add_parser(
        "daemon", help="Long-running: watch the configured source directory, deliver vCons."
    )
    return p


def main() -> None:
    parser = _build_parser()
    args = parser.parse_args()
    if args.cmd == "convert":
        sys.exit(_cmd_convert(args))
    # Default: daemon mode (matches template behavior).
    sys.exit(_cmd_daemon(args))
