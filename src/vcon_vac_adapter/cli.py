"""Entry point.

Two subcommands:
  * `vac-adapter convert <session-file> --platform <p> --out <path>`
      one-shot: parse a saved session file, write the vCon JSON to --out.
  * `vac-adapter daemon` (or no subcommand)
      long-running: load config, start health/metrics server, watch the
      configured source platform, build vCons, post to webhooks.
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

from .config import load_config
from .health_server import HealthServer
from .ir import Session
from .session_vcon import build_vcon
from .sources import claude_code as src_claude_code

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


def _cmd_convert(args: argparse.Namespace) -> int:
    session = _parse_one(Path(args.input), args.platform)
    v = build_vcon(
        session,
        granularity=args.granularity,
        include_lawful_basis=not args.no_lawful_basis,
        vac_encoding=args.vac_encoding,
        critical_agent_session=args.critical_agent_session,
    )
    if args.sign_key:
        v.sign(Path(args.sign_key).read_bytes())
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(v.dumps())
    log.info("wrote", path=str(out), bytes=out.stat().st_size)
    return 0


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

    # TODO: per-platform source watcher. v0.1 wires the daemon stub; concrete
    # source.stream() loops land per platform as their parsers harden.
    log.info(
        "daemon ready; configure config.adapter.source_platform "
        "and watch the corresponding source path"
    )

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)

    await stop.wait()
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

    sub.add_parser("daemon", help="Long-running: watch source platform, post vCons via webhook.")
    return p


def main() -> None:
    parser = _build_parser()
    args = parser.parse_args()
    if args.cmd == "convert":
        sys.exit(_cmd_convert(args))
    # Default: daemon mode (matches template behavior).
    sys.exit(_cmd_daemon(args))
