"""Parse Claude Code JSONL session transcripts into the IR.

A Claude Code session lives at:
  ~/.claude/projects/<encoded-cwd>/<session-id>.jsonl

Each line is one event, typically with shape:
  {
    "type": "user" | "assistant" | "system" | "queue-operation" | "ai-title" | "last-prompt",
    "message": {
      "role": "user" | "assistant",
      "content": str  OR  [{type: "text"|"thinking"|"tool_use"|"tool_result", ...}, ...]
    },
    "uuid": str,
    "parentUuid": str | null,
    "timestamp": ISO-8601 str,
    "sessionId": str,
    "cwd": str,
    "gitBranch": str | null,
    "version": str,
    ...
  }

Sub-agents are recognized by Task tool calls whose tool_input carries a
`subagent_type` field — they share the same JSONL stream but their parent_id
chain branches at the Task call.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Iterable
from datetime import datetime
from pathlib import Path
from typing import Any

from ..file_changes import derive_file_changes
from ..git_utils import current_branch, head_commit
from ..ir import AgentEnv, AgentRef, Entry, Session

DEFAULT_PROVIDER = "anthropic"
DEFAULT_RECORDER = "claude-code"


def _parse_ts(s: Any) -> datetime:
    if isinstance(s, datetime):
        return s
    if isinstance(s, str):
        # JSONL uses "...Z" or "...+00:00"
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        return datetime.fromisoformat(s)
    return datetime.utcnow()


def _stable_uuid(raw: str | None, fallback: str) -> str:
    return raw or str(uuid.uuid5(uuid.NAMESPACE_OID, fallback))


def _normalize_blocks(content: Any) -> list[dict[str, Any]]:
    """Coerce message.content into a list of typed blocks."""
    if content is None:
        return []
    if isinstance(content, str):
        return [{"type": "text", "text": content}]
    if isinstance(content, list):
        out: list[dict[str, Any]] = []
        for blk in content:
            if isinstance(blk, str):
                out.append({"type": "text", "text": blk})
            elif isinstance(blk, dict):
                out.append(blk)
        return out
    return []


def parse_lines(
    lines: Iterable[str],
    *,
    session_id: str | None = None,
    cwd: str | None = None,
    model_id: str = "claude-unknown",
    user_name: str = "User",
    user_validation: str | None = None,
) -> Session:
    """Parse JSONL lines into an IR Session."""
    events: list[dict[str, Any]] = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError:
            continue

    # Skip control records; they're useful but not VAC entries.
    SKIP = {"queue-operation"}
    events = [ev for ev in events if ev.get("type") not in SKIP]
    if not events:
        raise ValueError("no usable events in JSONL stream")

    # Session metadata from the first event.
    first = events[0]
    sid = session_id or first.get("sessionId") or first.get("session_id") or str(uuid.uuid4())
    started_at = _parse_ts(first.get("timestamp"))
    ended_at = _parse_ts(events[-1].get("timestamp"))
    cwd_str = cwd or first.get("cwd") or None
    git_branch = first.get("gitBranch") or (current_branch(cwd_str) if cwd_str else None)
    git_commit = head_commit(cwd_str) if cwd_str else None
    recorder_version = first.get("version", "")
    primary_model: Any = model_id
    for ev in events:
        cand = ev.get("model") or ev.get("modelId")
        if isinstance(cand, dict):
            cand = cand.get("id") or cand.get("name")
        if cand:
            primary_model = cand
            break

    primary_agent = AgentRef(
        agent_id="agent-claude-code-0",
        model_id=str(primary_model),
        provider=DEFAULT_PROVIDER,
        recording_agent=f"{DEFAULT_RECORDER}/{recorder_version}"
        if recorder_version
        else DEFAULT_RECORDER,
        name=str(primary_model),
        environment=AgentEnv(cwd=cwd_str, vcs_branch=git_branch, vcs_commit=git_commit),
    )
    agents: dict[str, AgentRef] = {primary_agent.agent_id: primary_agent}

    entries: list[Entry] = []
    tool_results_by_use_id: dict[str, str] = {}
    last_message_uuid_by_agent: dict[str, str | None] = {primary_agent.agent_id: None}
    tool_call_owner: dict[str, str] = {}  # tool_use_id -> entry.entry_id (the call)
    subagent_seq = 0

    for ev in events:
        ts = _parse_ts(ev.get("timestamp"))
        ev_uuid = ev.get("uuid")
        parent_uuid = ev.get("parentUuid")
        ev_type = ev.get("type")
        msg = ev.get("message") or {}
        blocks = _normalize_blocks(msg.get("content"))

        # Default ownership: primary agent. Sub-agent inheritance is handled
        # when Task tool calls are observed below.
        agent_id = primary_agent.agent_id

        if ev_type == "user":
            text_blocks = [b for b in blocks if b.get("type") == "text"]
            tool_result_blocks = [b for b in blocks if b.get("type") == "tool_result"]

            for b in text_blocks:
                eid = _stable_uuid(ev_uuid, f"user:{sid}:{ts.isoformat()}:{b.get('text', '')[:32]}")
                entries.append(
                    Entry(
                        entry_id=eid,
                        kind="message",
                        role="user",
                        text=b.get("text", ""),
                        timestamp=ts,
                        agent_id=agent_id,
                        parent_id=parent_uuid,
                    )
                )
                last_message_uuid_by_agent[agent_id] = eid

            for b in tool_result_blocks:
                tu_id = b.get("tool_use_id") or b.get("id") or ""
                content = b.get("content")
                if isinstance(content, list):
                    text_parts = []
                    for c in content:
                        if isinstance(c, dict) and c.get("type") == "text":
                            text_parts.append(c.get("text", ""))
                        elif isinstance(c, str):
                            text_parts.append(c)
                    output: Any = "\n".join(text_parts) if text_parts else content
                else:
                    output = content
                if isinstance(output, str):
                    tool_results_by_use_id[tu_id] = output
                eid = _stable_uuid(ev_uuid, f"tool_result:{sid}:{tu_id}") + f":{tu_id[:8]}"
                entries.append(
                    Entry(
                        entry_id=eid,
                        kind="tool_result",
                        tool_use_id=tu_id,
                        tool_output=output,
                        is_error=bool(b.get("is_error")),
                        timestamp=ts,
                        agent_id=agent_id,
                        parent_id=tool_call_owner.get(tu_id) or parent_uuid,
                    )
                )

        elif ev_type == "assistant":
            assistant_eid: str | None = None
            for b in blocks:
                btype = b.get("type")
                if btype == "thinking":
                    eid = (
                        _stable_uuid(ev_uuid, f"reasoning:{sid}:{ts.isoformat()}")
                        + f":r{len(entries)}"
                    )
                    entries.append(
                        Entry(
                            entry_id=eid,
                            kind="reasoning",
                            text=b.get("thinking", ""),
                            timestamp=ts,
                            agent_id=agent_id,
                            parent_id=parent_uuid,
                        )
                    )
                elif btype == "text":
                    eid = (
                        _stable_uuid(ev_uuid, f"assistant:{sid}:{ts.isoformat()}")
                        + f":a{len(entries)}"
                    )
                    entries.append(
                        Entry(
                            entry_id=eid,
                            kind="message",
                            role="assistant",
                            text=b.get("text", ""),
                            timestamp=ts,
                            agent_id=agent_id,
                            parent_id=parent_uuid,
                        )
                    )
                    last_message_uuid_by_agent[agent_id] = eid
                    assistant_eid = eid
                elif btype == "tool_use":
                    tu_id = b.get("id", "")
                    name = b.get("name", "")
                    tinput = b.get("input") or {}
                    eid = (
                        _stable_uuid(ev_uuid, f"tool_call:{sid}:{tu_id or name}")
                        + f":t{len(entries)}"
                    )
                    entries.append(
                        Entry(
                            entry_id=eid,
                            kind="tool_call",
                            tool_name=name,
                            tool_use_id=tu_id,
                            tool_input=tinput,
                            timestamp=ts,
                            agent_id=agent_id,
                            parent_id=assistant_eid or parent_uuid,
                        )
                    )
                    tool_call_owner[tu_id] = eid

                    # Detect Task tool calls spawning a sub-agent.
                    if name == "Task" and isinstance(tinput, dict) and tinput.get("subagent_type"):
                        subagent_seq += 1
                        sub_id = f"agent-claude-code-sub-{subagent_seq}"
                        agents[sub_id] = AgentRef(
                            agent_id=sub_id,
                            model_id=str(primary_model),
                            provider=DEFAULT_PROVIDER,
                            recording_agent=primary_agent.recording_agent,
                            name=str(tinput.get("subagent_type")),
                            parent_agent_id=primary_agent.agent_id,
                            environment=primary_agent.environment,
                        )

        elif ev_type in ("system", "ai-title", "last-prompt"):
            eid = _stable_uuid(ev_uuid, f"event:{sid}:{ts.isoformat()}:{ev_type}")
            entries.append(
                Entry(
                    entry_id=eid,
                    kind="event",
                    event_type=str(ev_type),
                    timestamp=ts,
                    agent_id=agent_id,
                    parent_id=parent_uuid,
                    meta={
                        k: v
                        for k, v in ev.items()
                        if k not in {"uuid", "parentUuid", "timestamp", "type"}
                    },
                )
            )

    file_changes = derive_file_changes(
        entries,
        commit=git_commit,
        tool_results_by_use_id=tool_results_by_use_id,
    )

    return Session(
        session_id=sid,
        started_at=started_at,
        ended_at=ended_at,
        user_party={
            "name": user_name,
            "role": "user",
            # draft-ietf-vcon-vcon-core-04 §4.2.11: a Claude Code session's
            # "user" JSONL events are the human operator's own turns, so
            # `type: "person"` is unambiguous here (unlike otel.py, where a
            # "user" span carries no such guarantee).
            "type": "person",
            **({"validation": user_validation} if user_validation else {}),
        },
        agents=list(agents.values()),
        entries=entries,
        file_changes=file_changes,
        source_platform="claude_code",
        source_protocol_version=recorder_version,
        raw_meta={"cwd": cwd_str, "git_branch": git_branch, "git_commit": git_commit},
    )


def parse_file(
    path: str | Path,
    **kwargs: Any,
) -> Session:
    """Parse a single .jsonl session file into an IR Session."""
    p = Path(path)
    with p.open("r", encoding="utf-8") as f:
        return parse_lines(f, **kwargs)
