"""Smoke + compliance tests for the VAC-emitting session vCon builder."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from vcon import Vcon

from vcon_vac_adapter.file_changes import derive_file_changes
from vcon_vac_adapter.ir import AgentEnv, AgentRef, Entry, Session
from vcon_vac_adapter.session_vcon import build_vcon
from vcon_vac_adapter.vac_builder import VAC_SCHEMA_URL
from vcon_vac_adapter.vcon_builder import LawfulBasisConfig, json_body

from .helpers import DEFAULT_PURPOSES

_SYNTHETIC_LAWFUL_BASIS_CFG = LawfulBasisConfig(
    lawful_basis="legitimate_interests",
    purposes=DEFAULT_PURPOSES,
)


def _fixture_session(*, with_subagent: bool = False, with_filechange: bool = True) -> Session:
    started = datetime(2026, 5, 22, 18, 0, tzinfo=UTC)
    agents = [
        AgentRef(
            agent_id="agent-claude-0",
            model_id="claude-opus-4-7",
            provider="anthropic",
            recording_agent="claude-code/1.2.0",
            name="Claude Opus 4.7",
            environment=AgentEnv(
                cwd="/Users/example/proj",
                vcs_branch="main",
                vcs_commit="abc123def456",
            ),
        )
    ]
    if with_subagent:
        agents.append(
            AgentRef(
                agent_id="agent-claude-sub-1",
                model_id="claude-haiku-4-5",
                provider="anthropic",
                recording_agent="claude-code/1.2.0",
                name="Sub-Agent",
                parent_agent_id="agent-claude-0",
            )
        )
    entries = [
        Entry(
            entry_id="m1",
            kind="message",
            role="user",
            text="List files in the cwd",
            timestamp=started,
            agent_id="agent-claude-0",
        ),
        Entry(
            entry_id="m2",
            kind="message",
            role="assistant",
            text="I'll list them.",
            timestamp=started,
            agent_id="agent-claude-0",
            parent_id="m1",
        ),
        Entry(
            entry_id="tc1",
            kind="tool_call",
            tool_name="Bash",
            tool_use_id="tu_1",
            tool_input={"command": "ls"},
            timestamp=started,
            agent_id="agent-claude-0",
            parent_id="m2",
        ),
        Entry(
            entry_id="tr1",
            kind="tool_result",
            tool_use_id="tu_1",
            tool_output="README.md\nsrc/",
            is_error=False,
            timestamp=started,
            agent_id="agent-claude-0",
            parent_id="tc1",
        ),
    ]
    if with_filechange:
        entries.append(
            Entry(
                entry_id="tc2",
                kind="tool_call",
                tool_name="Write",
                tool_use_id="tu_2",
                tool_input={"file_path": "src/foo.py", "content": "x = 1\n"},
                timestamp=started,
                agent_id="agent-claude-0",
                parent_id="m2",
            )
        )
    file_changes = derive_file_changes(entries, commit="abc123def456")
    return Session(
        session_id="sess-test-1",
        started_at=started,
        ended_at=started,
        user_party={"name": "Test User", "role": "user", "validation": "synthetic"},
        agents=agents,
        entries=entries,
        file_changes=file_changes,
        source_platform="claude_code",
    )


def _build(**kwargs) -> Vcon:
    return build_vcon(
        _fixture_session(**kwargs),
        lawful_basis_cfg=_SYNTHETIC_LAWFUL_BASIS_CFG,
    )


# --- spec / agent_session compliance ---


def test_extensions_includes_agent_session():
    v = _build()
    assert "agent_session" in v.vcon_dict["extensions"]


def test_extensions_includes_lawful_basis_when_set():
    v = _build()
    assert "lawful_basis" in v.vcon_dict["extensions"]


def test_agent_party_meta_has_required_fields():
    v = _build()
    agent_parties = [p for p in v.vcon_dict["parties"] if p.get("role") == "agent"]
    assert len(agent_parties) == 1
    meta = agent_parties[0]["meta"]["agent_session"]
    assert meta["model_id"] == "claude-opus-4-7"
    assert meta["provider"] == "anthropic"
    assert meta["recording_agent"] == "claude-code/1.2.0"
    assert "environment" in meta and meta["environment"]["cwd"] == "/Users/example/proj"


def test_subagent_has_parent_agent_id():
    v = build_vcon(_fixture_session(with_subagent=True))
    sub = next(p for p in v.vcon_dict["parties"] if p.get("name") == "Sub-Agent")
    assert sub["meta"]["agent_session"]["parent_agent_id"] == "agent-claude-0"


def test_analysis_has_agent_trace_with_vendor_schema_encoding():
    v = _build()
    analyses = v.vcon_dict.get("analysis", [])
    traces = [a for a in analyses if a.get("type") == "agent_trace"]
    assert len(traces) == 1
    t = traces[0]
    assert t["vendor"]
    assert t["schema"] == VAC_SCHEMA_URL
    assert t["encoding"] in {"json", "base64url"}


def test_analysis_body_is_valid_vac_record():
    v = _build()
    t = next(a for a in v.vcon_dict["analysis"] if a["type"] == "agent_trace")
    rec = json_body(t)
    var = rec["verifiable-agent-record"]
    assert var["version"]
    trace = var["session-trace"]
    assert isinstance(trace["entries"], list) and len(trace["entries"]) >= 4
    for e in trace["entries"]:
        assert "entry-id" in e and "kind" in e and "timestamp" in e


def test_vac_entry_ids_are_deterministic_across_reruns():
    s = _fixture_session()
    v1 = build_vcon(s)
    v2 = build_vcon(s)
    rec1 = json_body(next(a for a in v1.vcon_dict["analysis"] if a["type"] == "agent_trace"))
    rec2 = json_body(next(a for a in v2.vcon_dict["analysis"] if a["type"] == "agent_trace"))
    ids1 = [e["entry-id"] for e in rec1["verifiable-agent-record"]["session-trace"]["entries"]]
    ids2 = [e["entry-id"] for e in rec2["verifiable-agent-record"]["session-trace"]["entries"]]
    assert ids1 == ids2


def test_file_change_attachment_has_required_fields():
    v = _build()
    fcs = [a for a in v.vcon_dict.get("attachments", []) if a.get("purpose") == "agent_file_change"]
    assert fcs, "expected at least one agent_file_change attachment"
    fc = fcs[0]
    assert "party" in fc and "dialog" in fc
    assert fc["encoding"] == "json"
    assert fc["content_hash"].startswith("sha512-")
    # -04 §2.3.2: encoding: "json" means `body` IS the value, not a string.
    assert not isinstance(fc["body"], str)
    body = json_body(fc)
    assert body["path"] == "src/foo.py"
    assert body["operation"] == "update"


def test_agent_environment_attachment_present_per_agent():
    v = _build()
    envs = [
        a for a in v.vcon_dict.get("attachments", []) if a.get("purpose") == "agent_environment"
    ]
    assert len(envs) == 1
    assert not isinstance(envs[0]["body"], str)
    body = json_body(envs[0])
    assert body["cwd"] == "/Users/example/proj"


def test_lawful_basis_attachment_uses_purpose_not_type():
    v = _build()
    lbs = [a for a in v.vcon_dict.get("attachments", []) if a.get("purpose") == "lawful_basis"]
    assert len(lbs) == 1
    lb = lbs[0]
    assert "type" not in lb  # NEVER the legacy `type` field
    # -04 §2.3.2: encoding: "json" means `body` IS the value, not a string.
    assert not isinstance(lb["body"], str)
    assert lb["mediatype"] == "application/json"
    body = json_body(lb)
    assert body["lawful_basis"] == "legitimate_interests"
    purposes = [pg["purpose"] for pg in body["purpose_grants"]]
    assert "agent_session_recording" in purposes
    assert "agent_session_analysis" in purposes
    assert "agent_session_redistribution" in purposes


def test_lawful_basis_attachment_omitted_when_unset(monkeypatch):
    for var in (
        "LAWFUL_BASIS",
        "LAWFUL_BASIS_PURPOSE",
        "LAWFUL_BASIS_JURISDICTION",
        "LAWFUL_BASIS_EXPIRATION",
        "LAWFUL_BASIS_PROOF_MECHANISM",
        "LAWFUL_BASIS_PROOF_DESCRIPTION",
    ):
        monkeypatch.delenv(var, raising=False)
    v = build_vcon(_fixture_session(), lawful_basis_cfg=LawfulBasisConfig(lawful_basis=None))
    lbs = [a for a in v.vcon_dict.get("attachments", []) if a.get("purpose") == "lawful_basis"]
    assert lbs == []
    assert "lawful_basis" not in v.vcon_dict.get("extensions", [])


def test_no_legacy_schema_version_field():
    v = _build()
    for a in v.vcon_dict.get("analysis", []):
        assert "schema_version" not in a


def test_no_legacy_type_on_core_attachments():
    v = _build()
    for a in v.vcon_dict.get("attachments", []):
        if a.get("purpose") in {"agent_file_change", "agent_environment"}:
            assert "type" not in a


def test_per_tool_call_granularity_emits_one_analysis_per_tool_call():
    s = _fixture_session()
    v = build_vcon(s, granularity="per_tool_call")
    traces = [a for a in v.vcon_dict.get("analysis", []) if a["type"] == "agent_trace"]
    tool_calls = [e for e in s.entries if e.kind == "tool_call"]
    assert len(traces) == len(tool_calls)


def test_round_trip_serializes_and_parses():
    v = _build()
    data = v.dumps()
    Vcon.build_from_json(data)


def test_cbor_encoding_when_requested():
    pytest.importorskip("cbor2")
    v = build_vcon(_fixture_session(), vac_encoding="cbor")
    t = next(a for a in v.vcon_dict["analysis"] if a["type"] == "agent_trace")
    assert t["encoding"] == "base64url"
    assert t.get("mediatype") == "application/cbor"
