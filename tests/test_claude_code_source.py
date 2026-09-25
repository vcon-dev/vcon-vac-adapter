"""End-to-end test: Claude Code JSONL → IR → vCon with agent_session + VAC."""

from __future__ import annotations

from pathlib import Path

from vcon import Vcon

from vcon_vac_adapter.session_vcon import build_vcon
from vcon_vac_adapter.sources.claude_code import parse_file
from vcon_vac_adapter.vcon_builder import json_body

FIXTURES = Path(__file__).parent / "fixtures" / "claude_code"


def test_simple_session_parses_to_ir():
    s = parse_file(FIXTURES / "simple_session.jsonl")
    assert s.session_id == "sess-abc"
    assert s.source_platform == "claude_code"
    assert len(s.agents) == 1
    assert s.agents[0].environment.cwd == "/tmp/proj"
    # entries: u1 text, a1 reasoning + text + tool_use, u2 tool_result, a2 text = 6
    kinds = [e.kind for e in s.entries]
    assert kinds.count("message") >= 3  # u1, a1-text, a2
    assert kinds.count("reasoning") == 1
    assert kinds.count("tool_call") == 1
    assert kinds.count("tool_result") == 1
    # file_changes from Write
    assert len(s.file_changes) == 1
    assert s.file_changes[0].path == "src/foo.py"
    assert s.file_changes[0].operation == "update"


def test_simple_session_emits_valid_vcon():
    s = parse_file(FIXTURES / "simple_session.jsonl")
    v = build_vcon(s)
    # Spec checks
    assert v.vcon_dict["vcon"] == "0.4.0"
    assert "agent_session" in v.vcon_dict["extensions"]
    # agent_trace analysis present
    traces = [a for a in v.vcon_dict.get("analysis", []) if a["type"] == "agent_trace"]
    assert len(traces) == 1
    body = json_body(traces[0])
    entries = body["verifiable-agent-record"]["session-trace"]["entries"]
    kinds = [e["kind"] for e in entries]
    assert "tool_call" in kinds
    assert "tool_result" in kinds
    # file_change attachment present
    fcs = [a for a in v.vcon_dict.get("attachments", []) if a.get("purpose") == "agent_file_change"]
    assert len(fcs) == 1
    body = json_body(fcs[0])
    assert body["path"] == "src/foo.py"
    # Round trip
    Vcon.build_from_json(v.dumps())


def test_multiedit_and_bash_rm_produce_file_changes():
    s = parse_file(FIXTURES / "multiedit_session.jsonl")
    paths = {(fc.path, fc.operation) for fc in s.file_changes}
    assert ("src/util.py", "update") in paths
    assert ("old.log", "delete") in paths
    # MultiEdit with 2 edits → 2 file changes
    multi = [fc for fc in s.file_changes if fc.path == "src/util.py"]
    assert len(multi) == 2
    v = build_vcon(s)
    fc_atts = [
        a for a in v.vcon_dict.get("attachments", []) if a.get("purpose") == "agent_file_change"
    ]
    assert len(fc_atts) == 3


def test_subagent_task_produces_two_agent_parties():
    s = parse_file(FIXTURES / "subagent_task.jsonl")
    assert len(s.agents) == 2
    sub = next(a for a in s.agents if a.parent_agent_id is not None)
    assert sub.parent_agent_id == "agent-claude-code-0"
    assert sub.name == "general-purpose"
    v = build_vcon(s)
    agent_parties = [p for p in v.vcon_dict["parties"] if p.get("role") == "agent"]
    assert len(agent_parties) == 2
    sub_party = next(p for p in agent_parties if p.get("name") == "general-purpose")
    assert sub_party["meta"]["agent_session"]["parent_agent_id"] == "agent-claude-code-0"
