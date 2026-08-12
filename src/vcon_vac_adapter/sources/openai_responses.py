"""OpenAI Responses API → IR via vcon-mcp-adapters bridge."""

from __future__ import annotations

from typing import Any

from ..ir import Session
from .from_mcp_session import from_mcp_session


def parse_trace(request: dict[str, Any], response: dict[str, Any]) -> Session:
    """Convert an OpenAI Responses API request/response pair into a Session.

    Requires `vcon-mcp-adapters` installed; raises ImportError otherwise.
    """
    from vcon_mcp_adapters.adapters.openai_responses import from_trace  # type: ignore[import-not-found]

    mcp = from_trace(request, response)
    return from_mcp_session(mcp, platform="openai_responses")
