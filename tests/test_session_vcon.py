"""Smoke + compliance tests for the VAC-emitting session vCon builder."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from vcon import Vcon

from vcon_vac_adapter.file_changes import derive_file_changes
from vcon_vac_adapter.ir import AgentEnv, AgentRef, Entry, Session
from vcon_vac_adapter.session_vcon import build_vcon
from vcon_vac_adapter.vac_builder import VAC_SCHEMA_URL
from vcon_vac_adapter.vcon_builder import LawfulBasisConfig, add_lawful_basis, json_body

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


def test_agent_party_is_typed_bot_with_org_from_provider():
    """draft-ietf-vcon-vcon-core-04 §4.2.11/§4.2.12: an AgentRef always
    represents an automated party, so `type: "bot"` is unconditional; `org`
    is set from the source-derived `AgentRef.provider`."""
    v = _build()
    agent_parties = [p for p in v.vcon_dict["parties"] if p.get("role") == "agent"]
    assert agent_parties[0]["type"] == "bot"
    assert agent_parties[0]["org"] == "anthropic"


def test_user_party_type_passes_through_unmodified():
    """`build_vcon` never invents a `type` for the user party — it only
    forwards whatever the IR's `Session.user_party` dict already carries
    (set by the source parser, per source-specific ambiguity rules)."""
    v = _build()
    assert "type" not in v.vcon_dict["parties"][0]


def test_agent_party_org_omitted_when_provider_unknown():
    s = _fixture_session()
    s.agents[0] = AgentRef(
        agent_id=s.agents[0].agent_id,
        model_id=s.agents[0].model_id,
        provider="unknown",
        recording_agent=s.agents[0].recording_agent,
        name=s.agents[0].name,
        environment=s.agents[0].environment,
    )
    v = build_vcon(s, lawful_basis_cfg=_SYNTHETIC_LAWFUL_BASIS_CFG)
    agent_party = next(p for p in v.vcon_dict["parties"] if p.get("role") == "agent")
    assert agent_party["type"] == "bot"
    assert "org" not in agent_party


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


# --- analysis[].attachment (CON-1104) ---


def _attachment_indices(v: Vcon, *, purpose: str) -> list[int]:
    return [
        i for i, a in enumerate(v.vcon_dict.get("attachments", [])) if a.get("purpose") == purpose
    ]


def test_session_granularity_trace_links_to_all_file_change_and_environment_attachments():
    v = _build()  # with_filechange=True, single agent with a non-empty environment
    (fc_idx,) = _attachment_indices(v, purpose="agent_file_change")
    (env_idx,) = _attachment_indices(v, purpose="agent_environment")
    t = next(a for a in v.vcon_dict["analysis"] if a["type"] == "agent_trace")
    assert t["attachment"] == sorted([fc_idx, env_idx])


def test_session_granularity_trace_attachment_is_bare_int_when_only_one_match():
    v = _build(with_filechange=False)  # no file changes -> only the environment attachment
    (env_idx,) = _attachment_indices(v, purpose="agent_environment")
    t = next(a for a in v.vcon_dict["analysis"] if a["type"] == "agent_trace")
    assert t["attachment"] == env_idx


def test_per_tool_call_granularity_links_each_trace_to_its_own_file_change():
    s = _fixture_session()  # tc1 = Bash "ls" (no file change), tc2 = Write src/foo.py
    v = build_vcon(s, granularity="per_tool_call")
    (fc_idx,) = _attachment_indices(v, purpose="agent_file_change")
    (env_idx,) = _attachment_indices(v, purpose="agent_environment")
    traces = [a for a in v.vcon_dict["analysis"] if a["type"] == "agent_trace"]
    assert len(traces) == 2

    def _body_entries(t):
        return json_body(t)["verifiable-agent-record"]["session-trace"]["entries"]

    tc1_trace = next(
        t
        for t in traces
        if _body_entries(t)[0]["kind"] == "tool_call"
        and _body_entries(t)[0].get("tool-name") == "Bash"
    )
    tc2_trace = next(t for t in traces if _body_entries(t)[0].get("tool-name") == "Write")

    # tc1 (Bash "ls") produced no file change: derived only from the
    # environment attachment (every trace embeds the full agent+env list).
    assert tc1_trace["attachment"] == env_idx
    # tc2 (Write) produced a file change: derived from both.
    assert tc2_trace["attachment"] == sorted([fc_idx, env_idx])


def test_no_attachment_field_when_trace_derived_from_nothing():
    # No file changes and environment attachments disabled: the trace is
    # derived from dialog only, so `attachment` must be omitted entirely
    # (draft-ietf-vcon-vcon-core-04 §4.5.3: optional only in that case).
    v = build_vcon(
        _fixture_session(with_filechange=False),
        include_environment_attachments=False,
        lawful_basis_cfg=_SYNTHETIC_LAWFUL_BASIS_CFG,
    )
    assert v.vcon_dict.get("attachments", []) == [
        a for a in v.vcon_dict["attachments"] if a.get("purpose") == "lawful_basis"
    ]
    t = next(a for a in v.vcon_dict["analysis"] if a["type"] == "agent_trace")
    assert "attachment" not in t


def test_lawful_basis_appended_after_build_does_not_shift_analysis_attachment_indices():
    """A caller (e.g. a CLI `finalize` step) may build with
    `include_lawful_basis=False` and append `lawful_basis` itself afterward.
    Since it always lands after every attachment an analysis might reference,
    already-resolved `attachment` indices on the analysis must still be
    correct once it's added.
    """
    v = build_vcon(_fixture_session(), include_lawful_basis=False)
    t = next(a for a in v.vcon_dict["analysis"] if a["type"] == "agent_trace")
    (fc_idx,) = _attachment_indices(v, purpose="agent_file_change")
    (env_idx,) = _attachment_indices(v, purpose="agent_environment")
    expected = sorted([fc_idx, env_idx])
    assert t["attachment"] == expected

    added = add_lawful_basis(v, _SYNTHETIC_LAWFUL_BASIS_CFG, granted_at="2026-05-22T18:00:00+00:00")
    assert added

    # Same analysis dict, still correct: `attachment` indices are unaffected
    # by lawful_basis landing after them.
    assert t["attachment"] == expected
    for idx in expected:
        assert v.vcon_dict["attachments"][idx].get("purpose") in {
            "agent_file_change",
            "agent_environment",
        }
    assert v.vcon_dict["attachments"][-1]["purpose"] == "lawful_basis"
