"""OpenAI Agents SDK runtime → IR via vcon-mcp-adapters bridge.

Two entry points:
  * `parse_spans(spans)`: post-hoc projection of a finished trace.
  * `VacTracingProcessor`: a TracingProcessor for live integration. Its
    `on_trace_end` callback enqueues the finished MCPSession into an asyncio
    Queue; the daemon's consumer task converts to Session + vCon + webhook
    delivery off the SDK thread. Zero-IO discipline inside the callback.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any

from ..ir import Session
from .from_mcp_session import from_mcp_session


def parse_spans(spans: list[Any]) -> Session:
    from vcon_mcp_adapters.adapters.openai_agents import (
        from_spans,
    )

    mcp = from_spans(spans)
    return from_mcp_session(mcp, platform="openai_agents")


class VacTracingProcessor:
    """Live trace processor. Wire into `agents.set_trace_processors([...])`.

    On `on_trace_end`, push the MCPSession into `queue`. The daemon consumes
    off-thread to keep this callback non-blocking.
    """

    def __init__(self, queue: asyncio.Queue[Session]) -> None:
        self._queue = queue
        try:
            from vcon_mcp_adapters.adapters.openai_agents import (
                OpenAIAgentsAdapter,
            )
        except Exception as exc:
            raise ImportError("vcon-mcp-adapters not installed") from exc
        self._adapter = OpenAIAgentsAdapter()

    # The exact TracingProcessor interface lives in `agents` SDK; we mirror it
    # loosely here. Concrete adapters will wrap this.
    def on_trace_start(self, trace: Any) -> None:
        self._adapter.on_trace_start(trace)

    def on_span_start(self, span: Any) -> None:
        self._adapter.on_span_start(span)

    def on_span_end(self, span: Any) -> None:
        self._adapter.on_span_end(span)

    def on_trace_end(self, trace: Any) -> None:
        mcp = self._adapter.on_trace_end(trace)
        if mcp is None:
            return
        session = from_mcp_session(mcp, platform="openai_agents")
        # Enqueue without blocking the SDK thread.
        try:
            self._queue.put_nowait(session)
        except asyncio.QueueFull:
            # Drop on overflow rather than block; surface a counter from the
            # daemon's metrics.
            pass


def install_live_processor(
    queue: asyncio.Queue[Session],
    set_processors: Callable[[list[Any]], None] | None = None,
) -> VacTracingProcessor:
    """Convenience: instantiate and register the live processor.

    Pass `agents.set_trace_processors` as `set_processors` to wire it in.
    """
    proc = VacTracingProcessor(queue)
    if set_processors is not None:
        set_processors([proc])
    return proc
