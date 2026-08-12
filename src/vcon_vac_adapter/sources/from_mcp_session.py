"""Bridge: convert an mcp-adapters `MCPSession` into the VAC IR `Session`.

Used by the Anthropic, OpenAI Responses, and OpenAI Agents SDK source modules,
all of which rely on `vcon-mcp-adapters` to do the platform-native parsing.

Import is local + guarded so the rest of vcon-vac-adapter still works when
mcp-adapters is not installed.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from ..ir import AgentEnv, AgentRef, Entry, Session, SourcePlatform


def from_mcp_session(mcp_session: Any, *, platform: SourcePlatform = "anthropic") -> Session:
    """Project an mcp-adapters MCPSession (Pydantic model) into the IR Session.

    `mcp_session` is expected to be a `vcon_mcp_adapters.schema.MCPSession`
    instance. We use duck typing rather than importing the type to keep the
    runtime dependency optional.
    """
    sid: str = mcp_session.session_id
    model_id: str = getattr(mcp_session.model, "name", "unknown")
    provider: str = _provider_for_platform(platform)
    recorder: str = getattr(mcp_session.client, "framework", "") or platform.replace("_", "-")
    proto_v: str = getattr(mcp_session, "protocol_version", "") or ""

    ctx = mcp_session.context or {}
    env = AgentEnv(
        cwd=ctx.get("cwd"),
        vcs_branch=ctx.get("git_branch"),
        vcs_commit=ctx.get("git_commit"),
    )
    agent = AgentRef(
        agent_id=f"agent-{platform}-0",
        model_id=model_id,
        provider=provider,
        recording_agent=recorder,
        name=model_id,
        environment=env,
    )

    entries: list[Entry] = []
    for turn in getattr(mcp_session, "turns", []) or []:
        entries.extend(_turn_to_entries(turn, agent.agent_id))

    started = getattr(mcp_session, "start_time", None) or datetime.now(UTC)
    ended = getattr(mcp_session, "end_time", None)

    return Session(
        session_id=sid,
        started_at=started,
        ended_at=ended,
        user_party={"name": "User", "role": "user"},
        agents=[agent],
        entries=entries,
        file_changes=[],  # platform-specific extractors can populate after the fact
        source_platform=platform,
        source_protocol_version=proto_v,
        raw_meta={"context": ctx},
    )


def _provider_for_platform(platform: SourcePlatform) -> str:
    return {
        "claude_code": "anthropic",
        "anthropic": "anthropic",
        "openai_responses": "openai",
        "openai_agents": "openai",
    }[platform]


def _turn_to_entries(turn: Any, agent_id: str) -> list[Entry]:
    """Project an MCPSession Turn into one or more Entry rows.

    Turn shape from vcon_mcp_adapters.schema:
      - UserTurn: role="user", content blocks (text/image_ref/artifact_ref)
      - AssistantTurn: role="assistant", content blocks (text/tool_use/image_ref)
      - ToolTurn: role="tool", tool_use_id, content (output), is_error
      - SystemTurn: role="system", message
    """
    role = getattr(turn, "role", None) or getattr(turn, "type", "")
    ts = getattr(turn, "timestamp", None) or datetime.now(UTC)
    turn_id = getattr(turn, "turn_id", "") or getattr(turn, "id", "")
    parent_id = getattr(turn, "parent_id", None)

    if role == "user":
        text = _flatten_text(getattr(turn, "content", []))
        return [
            Entry(
                entry_id=turn_id or f"user-{ts.isoformat()}",
                kind="message",
                role="user",
                text=text,
                timestamp=ts,
                agent_id=agent_id,
                parent_id=parent_id,
            )
        ]
    if role == "assistant":
        out: list[Entry] = []
        text = _flatten_text(getattr(turn, "content", []))
        if text:
            out.append(
                Entry(
                    entry_id=turn_id or f"asst-{ts.isoformat()}",
                    kind="message",
                    role="assistant",
                    text=text,
                    timestamp=ts,
                    agent_id=agent_id,
                    parent_id=parent_id,
                )
            )
        for blk in getattr(turn, "content", []) or []:
            btype = getattr(blk, "type", None)
            if btype == "tool_use":
                out.append(
                    Entry(
                        entry_id=getattr(blk, "tool_use_id", "") or f"tc-{ts.isoformat()}",
                        kind="tool_call",
                        tool_name=getattr(blk, "name", ""),
                        tool_use_id=getattr(blk, "tool_use_id", ""),
                        tool_input=getattr(blk, "input", None) or {},
                        timestamp=ts,
                        agent_id=agent_id,
                        parent_id=turn_id or parent_id,
                    )
                )
        return out
    if role == "tool":
        return [
            Entry(
                entry_id=turn_id or f"tr-{ts.isoformat()}",
                kind="tool_result",
                tool_use_id=getattr(turn, "tool_use_id", ""),
                tool_output=getattr(turn, "content", None),
                is_error=bool(getattr(turn, "is_error", False)),
                duration_ms=getattr(turn, "duration_ms", None),
                timestamp=ts,
                agent_id=agent_id,
                parent_id=parent_id,
            )
        ]
    if role == "system":
        return [
            Entry(
                entry_id=turn_id or f"ev-{ts.isoformat()}",
                kind="event",
                event_type="system",
                timestamp=ts,
                agent_id=agent_id,
                parent_id=parent_id,
                meta={"message": getattr(turn, "message", None)},
            )
        ]
    return []


def _flatten_text(blocks: Any) -> str:
    if isinstance(blocks, str):
        return blocks
    if not blocks:
        return ""
    parts: list[str] = []
    for b in blocks:
        btype = getattr(b, "type", None)
        if btype == "text":
            parts.append(getattr(b, "text", "") or "")
    return "\n".join(p for p in parts if p)
