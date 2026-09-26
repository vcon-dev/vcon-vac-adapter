"""Party typing for the `from_mcp_session` bridge (anthropic, openai_responses,
openai_agents source modules) — draft-ietf-vcon-vcon-core-04 §4.2.11/§4.2.12.

`vcon_mcp_adapters` is an optional runtime dependency of this adapter (see
README "v0.1 platform support"), so these tests build a minimal duck-typed
stand-in for `MCPSession` rather than depending on that package being
installed.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from vcon_vac_adapter.session_vcon import build_vcon
from vcon_vac_adapter.sources.from_mcp_session import from_mcp_session


@dataclass
class _FakeModel:
    name: str = "claude-opus-4-7"


@dataclass
class _FakeClient:
    framework: str = ""


@dataclass
class _FakeMCPSession:
    session_id: str
    model: _FakeModel = field(default_factory=_FakeModel)
    client: _FakeClient = field(default_factory=_FakeClient)
    protocol_version: str = ""
    context: dict[str, Any] = field(default_factory=dict)
    turns: list[Any] = field(default_factory=list)
    start_time: datetime = field(default_factory=lambda: datetime(2026, 1, 1, tzinfo=UTC))
    end_time: datetime | None = None


def _agent_party(v):
    return next(p for p in v.vcon_dict["parties"] if p.get("role") == "agent")


def test_anthropic_platform_types_user_as_person_and_bot_as_org_anthropic():
    session = from_mcp_session(_FakeMCPSession(session_id="s1"), platform="anthropic")
    assert session.user_party["type"] == "person"
    v = build_vcon(session)
    assert v.vcon_dict["parties"][0]["type"] == "person"
    agent = _agent_party(v)
    assert agent["type"] == "bot"
    assert agent["org"] == "anthropic"


def test_openai_responses_platform_types_user_as_person_and_bot_as_org_openai():
    session = from_mcp_session(_FakeMCPSession(session_id="s2"), platform="openai_responses")
    assert session.user_party["type"] == "person"
    v = build_vcon(session)
    assert v.vcon_dict["parties"][0]["type"] == "person"
    agent = _agent_party(v)
    assert agent["type"] == "bot"
    assert agent["org"] == "openai"


def test_openai_agents_platform_leaves_user_untyped_but_bot_still_typed():
    """OpenAI Agents SDK is a multi-agent orchestration runtime: the
    top-level "user" turn can itself be another agent or an upstream
    service, so `type` is left off rather than guessed."""
    session = from_mcp_session(_FakeMCPSession(session_id="s3"), platform="openai_agents")
    assert "type" not in session.user_party
    v = build_vcon(session)
    assert "type" not in v.vcon_dict["parties"][0]
    agent = _agent_party(v)
    assert agent["type"] == "bot"
    assert agent["org"] == "openai"
