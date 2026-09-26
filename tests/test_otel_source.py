"""OTel GenAI spans → IR → signed vCon carrying a VAC record."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from vcon_vac_adapter.session_vcon import build_vcon
from vcon_vac_adapter.sources.otel import parse_spans
from vcon_vac_adapter.vcon_builder import json_body

FIXTURE = Path(__file__).parent / "fixtures" / "otel" / "genai_trace.json"


@pytest.fixture
def session():
    return parse_spans(json.loads(FIXTURE.read_text()))


def test_parses_agent_messages_and_tool_calls(session):
    assert session.session_id == "conv-9001"
    assert session.source_platform == "otel"

    agent = session.agents[0]
    assert (agent.provider, agent.model_id) == ("anthropic", "claude-opus-5")
    assert agent.recording_agent == "opentelemetry-instrumentation-anthropic"
    assert agent.environment.host == "runner-7"

    kinds = [e.kind for e in session.entries]
    assert kinds.count("message") == 2
    assert kinds.count("tool_call") == 1 and kinds.count("tool_result") == 1

    msgs = [e for e in session.entries if e.kind == "message"]
    assert [m.role for m in msgs] == ["user", "assistant"]
    assert msgs[0].text == "Is my order shipped?"

    call = next(e for e in session.entries if e.kind == "tool_call")
    assert call.tool_name == "lookup_order"
    assert call.tool_input == {"order_id": "A-77"}
    # The tool span inherits the model-bearing parent's agent.
    assert call.agent_id == agent.agent_id

    result = next(e for e in session.entries if e.kind == "tool_result")
    assert result.tool_output == {"status": "shipped", "date": "2026-08-04"}
    assert result.is_error is False


def test_builds_vcon_with_vac_record(session):
    v = build_vcon(session)
    d = v.vcon_dict

    assert d["vcon"] == "0.4.0"
    assert "agent_session" in d["extensions"]
    assert len(d["dialog"]) == 2

    # Party typing (draft-ietf-vcon-vcon-core-04 §4.2.11/§4.2.12): an OTel
    # span carries no role guarantee for the human party, so `type` stays
    # off there; the model party is still unambiguously a bot, with `org`
    # set from the source's own `gen_ai.provider.name` attribute.
    user_party = d["parties"][0]
    assert "type" not in user_party
    agent_party = next(p for p in d["parties"] if p.get("role") == "agent")
    assert agent_party["type"] == "bot"
    assert agent_party["org"] == "anthropic"

    analysis = next(a for a in d["analysis"] if a["type"] == "agent_trace")
    vac = json_body(analysis)["verifiable-agent-record"]
    assert analysis["schema"].endswith("draft-birkholz-verifiable-agent-conversations/")
    assert vac["session-trace"]["source-platform"] == "otel"
    entries = vac["session-trace"]["entries"]
    assert [e["kind"] for e in entries].count("tool_call") == 1
    assert next(e for e in entries if e["kind"] == "tool_call")["tool-name"] == "lookup_order"


def test_signed_vcon_verifies(session):
    pytest.importorskip("cryptography")
    from cryptography.hazmat.primitives import serialization
    from vcon import Vcon

    private_key, public_key = Vcon.generate_key_pair()
    v = build_vcon(session)
    v.sign(private_key)
    assert v.vcon_dict.get("signatures")

    pem = public_key.public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    assert Vcon(json.loads(v.dumps())).verify(pem)
