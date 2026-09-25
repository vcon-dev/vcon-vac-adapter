"""Validate a sample vCon (built via this adapter's own Claude Code parser +
`session_vcon.build_vcon`, with the `agent_session` extension and a
lawful-basis attachment, finalized via `finalize_vcon()`) against the
vendored official JSON schema, plus the non-negotiables the schema alone
doesn't fully enforce.

`assert_spec_compliant()` (and `_assert_body_shape()`) is copied verbatim
from `vcon-dev/vcon-adapter-template`'s `tests/test_spec_compliance.py`
(`vcon-dev/vcon-adapter-template`, pull request #1) — it is written to be
copied into any vCon-writing repo's test suite; it only depends on
`jsonschema` and the vendored schema file below it.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import jsonschema
import pytest
from vcon.dialog import Dialog
from vcon.party import Party

from vcon_vac_adapter.session_vcon import build_vcon
from vcon_vac_adapter.sources import claude_code as src_claude_code
from vcon_vac_adapter.vcon_builder import (
    LawfulBasisConfig,
    add_lawful_basis,
    finalize_vcon,
    new_vcon,
)

SCHEMA_PATH = Path(__file__).parent / "schema" / "vcon_json_schema.json"
FIXTURE_SESSION_PATH = Path(__file__).parent / "fixtures" / "claude_code" / "simple_session.jsonl"


def _walk(node: Any) -> Any:
    """Yield every dict found anywhere in a nested JSON-like structure."""
    if isinstance(node, dict):
        yield node
        for value in node.values():
            yield from _walk(value)
    elif isinstance(node, list):
        for item in node:
            yield from _walk(item)


def _assert_body_shape(obj: dict[str, Any], label: str) -> None:
    """draft-ietf-vcon-vcon-core-04 §2.3.2: with `encoding: "json"`, `body`
    IS the JSON value (object/array/number/bool/null) — never a
    `json.dumps()` string (a string body there would mean the value got
    double-encoded). With any other encoding (or none), `body` must be a
    plain string, same as -02.
    """
    if "body" not in obj:
        return
    if obj.get("encoding") == "json":
        assert not isinstance(obj["body"], str), (
            f"{label} `body` is a string under encoding: 'json' — should be "
            f"the raw JSON value per draft-ietf-vcon-vcon-core-04 §2.3.2, "
            f"not a json.dumps() string (looks double-encoded): {obj}"
        )
    else:
        assert isinstance(obj["body"], str), f"{label} `body` is not a string: {obj}"


def assert_spec_compliant(vcon_dict: dict[str, Any], schema_path: Path | str) -> None:
    """Assert `vcon_dict` conforms to the vendored vCon core JSON schema
    (draft-ietf-vcon-vcon-core-04) and to the project's non-negotiable
    field-naming rules.

    Raises `AssertionError` with the collected schema errors (if any), or on
    the first non-negotiable violation.
    """
    schema = json.loads(Path(schema_path).read_text())
    validator = jsonschema.Draft7Validator(schema, format_checker=jsonschema.FormatChecker())
    errors = sorted(validator.iter_errors(vcon_dict), key=lambda e: list(e.path))
    assert not errors, "schema violations:\n" + "\n".join(
        f"  {list(e.path)}: {e.message}" for e in errors
    )

    # `mediatype`, never `mimetype`, anywhere in the document.
    for node in _walk(vcon_dict):
        assert "mimetype" not in node, f"found legacy `mimetype` key in: {node}"

    # Every attachment carries purpose/start/party/dialog. Body is a string
    # unless encoding: "json", in which case it's the raw JSON value.
    for att in vcon_dict.get("attachments", []):
        assert "purpose" in att, f"attachment missing `purpose`: {att}"
        assert "type" not in att, f"attachment uses legacy `type` instead of `purpose`: {att}"
        assert "start" in att, f"attachment missing `start`: {att}"
        assert "party" in att, f"attachment missing `party`: {att}"
        assert "dialog" in att, f"attachment missing `dialog`: {att}"
        _assert_body_shape(att, "attachment")

    # Same body rule for analysis.
    for analysis in vcon_dict.get("analysis", []):
        _assert_body_shape(analysis, "analysis")
        assert "schema_version" not in analysis, f"legacy `schema_version` in: {analysis}"
        assert "vendor" in analysis, f"analysis missing required `vendor`: {analysis}"

    # No empty `meta`/`metadata`/`group`/`redacted` anywhere in the document.
    for node in _walk(vcon_dict):
        for key in ("meta", "metadata", "group", "redacted"):
            if key in node:
                assert node[key] not in ({}, [], None), f"empty `{key}` present in: {node}"


@pytest.fixture
def sample_vcon_dict() -> dict[str, Any]:
    """A representative vCon built through this adapter's real Claude Code
    parser + `session_vcon.build_vcon`, including the `agent_session`
    extension, a VAC `agent_trace`, file-change/environment attachments, and
    a lawful-basis attachment.
    """
    session = src_claude_code.parse_file(FIXTURE_SESSION_PATH)
    cfg = LawfulBasisConfig(
        lawful_basis="consent",
        purposes=("agent_session_recording", "agent_session_analysis"),
        jurisdiction="US-MA",
    )
    v = build_vcon(session, lawful_basis_cfg=cfg)

    # Same path a real delivery gets for free: WebhookDelivery and
    # ConserverDelivery both call finalize_vcon() before serializing. No
    # test-side stripping here — if finalize_vcon() regresses or is removed,
    # this fixture (and the test below) fails.
    return finalize_vcon(v.vcon_dict)


def test_schema_file_present_and_parses() -> None:
    schema = json.loads(SCHEMA_PATH.read_text())
    assert schema.get("title") or schema.get("$id"), "schema file looks empty/malformed"


def test_sample_vcon_with_lawful_basis_is_spec_compliant(
    sample_vcon_dict: dict[str, Any],
) -> None:
    assert_spec_compliant(sample_vcon_dict, SCHEMA_PATH)


def test_bare_new_vcon_is_spec_compliant() -> None:
    v = new_vcon()
    assert_spec_compliant(v.vcon_dict, SCHEMA_PATH)


def test_mimetype_key_fails_the_check() -> None:
    bad = new_vcon().vcon_dict
    bad["dialog"] = [{"type": "recording", "mimetype": "audio/wav"}]
    with pytest.raises(AssertionError, match="mimetype"):
        assert_spec_compliant(bad, SCHEMA_PATH)


def test_attachment_missing_dialog_index_fails_the_check() -> None:
    bad = new_vcon().vcon_dict
    bad["attachments"] = [
        {"purpose": "tags", "start": "2026-01-02T12:00:00Z", "party": 0, "body": "{}"}
    ]
    with pytest.raises((AssertionError, jsonschema.ValidationError)):
        assert_spec_compliant(bad, SCHEMA_PATH)


def test_non_string_analysis_body_fails_the_check() -> None:
    bad = new_vcon().vcon_dict
    bad["analysis"] = [
        {
            "type": "summary",
            "dialog": 0,
            "vendor": "v",
            "encoding": "none",
            "body": {"not": "a string"},
        }
    ]
    with pytest.raises(AssertionError, match="not a string"):
        assert_spec_compliant(bad, SCHEMA_PATH)


def test_json_encoded_attachment_with_string_body_fails_the_check() -> None:
    """encoding: "json" with a `body` that's a `json.dumps()` string is
    double-encoding under -04 §2.3.2 — `body` should be the raw JSON value.
    """
    bad = new_vcon().vcon_dict
    bad["attachments"] = [
        {
            "purpose": "tags",
            "start": "2026-01-02T12:00:00Z",
            "party": 0,
            "dialog": 0,
            "encoding": "json",
            "mediatype": "application/json",
            "body": json.dumps({"department": "sales"}),
        }
    ]
    with pytest.raises(AssertionError, match="double-encoded"):
        assert_spec_compliant(bad, SCHEMA_PATH)


def test_json_encoded_attachment_with_raw_value_body_passes() -> None:
    ok = new_vcon().vcon_dict
    ok["attachments"] = [
        {
            "purpose": "tags",
            "start": "2026-01-02T12:00:00Z",
            "party": 0,
            "dialog": 0,
            "encoding": "json",
            "mediatype": "application/json",
            "body": {"department": "sales"},
        }
    ]
    assert_spec_compliant(ok, SCHEMA_PATH)


def test_lawful_basis_attachment_added_via_add_lawful_basis_is_compliant() -> None:
    v = new_vcon()
    v.add_party(Party(name="Caller"))
    v.add_dialog(
        Dialog(
            type="text",
            start="2026-01-02T12:00:00Z",
            parties=[0],
            body="hi",
            encoding="none",
            mediatype="text/plain",
        )
    )
    cfg = LawfulBasisConfig(lawful_basis="consent", purposes=("recording",))
    added = add_lawful_basis(v, cfg, granted_at="2026-01-02T12:00:00Z", party=0, dialog=0)
    assert added is True
    assert_spec_compliant(finalize_vcon(v.vcon_dict), SCHEMA_PATH)
