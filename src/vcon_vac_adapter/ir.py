"""Intermediate Representation (IR) of an AI-agent session.

Sits between platform-specific parsers (Claude Code JSONL, Anthropic Messages
API, OpenAI Responses, OpenAI Agents SDK) and the VAC + vCon builders. Adds
what `vcon-mcp-adapters.MCPSession` lacks: multi-agent parties, parent/child
entry tree, and VAC-typed entry kinds (incl. `reasoning` and `event`).

Ordering invariant: `Session.entries[]` is depth-first, parent before children,
ties broken by timestamp then `entry_id` lexicographic.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal

EntryKind = Literal["message", "tool_call", "tool_result", "reasoning", "event"]
Operation = Literal["create", "update", "delete", "read"]
SourcePlatform = Literal["claude_code", "anthropic", "openai_responses", "openai_agents", "otel"]


@dataclass(frozen=True)
class AgentEnv:
    cwd: str | None = None
    vcs_branch: str | None = None
    vcs_commit: str | None = None
    host: str | None = None
    os: str | None = None


@dataclass(frozen=True)
class AgentRef:
    agent_id: str
    model_id: str
    provider: str
    recording_agent: str
    name: str | None = None
    parent_agent_id: str | None = None
    environment: AgentEnv = field(default_factory=AgentEnv)


@dataclass(frozen=True)
class Entry:
    entry_id: str
    kind: EntryKind
    timestamp: datetime
    agent_id: str
    parent_id: str | None = None
    # Payload (populated based on `kind`):
    text: str | None = None
    role: Literal["user", "assistant"] | None = None
    tool_name: str | None = None
    tool_use_id: str | None = None
    tool_input: dict[str, Any] | None = None
    tool_output: Any = None
    is_error: bool | None = None
    duration_ms: float | None = None
    event_type: str | None = None
    meta: dict[str, Any] | None = None


@dataclass(frozen=True)
class FileChange:
    entry_id: str
    agent_id: str
    path: str
    operation: Operation
    line_range: tuple[int, int] | None = None
    content_hash: str | None = None
    commit: str | None = None
    contributor: str | None = None
    diff_text: str | None = None


@dataclass
class Session:
    session_id: str
    started_at: datetime
    ended_at: datetime | None = None
    user_party: dict[str, Any] = field(default_factory=dict)
    agents: list[AgentRef] = field(default_factory=list)
    entries: list[Entry] = field(default_factory=list)
    file_changes: list[FileChange] = field(default_factory=list)
    source_platform: SourcePlatform = "claude_code"
    source_protocol_version: str = ""
    raw_meta: dict[str, Any] = field(default_factory=dict)
    lawful_basis: dict[str, Any] | None = None

    def agent_index(self) -> dict[str, int]:
        """Map agent_id → party index (1-based; user is party 0)."""
        return {a.agent_id: i + 1 for i, a in enumerate(self.agents)}
