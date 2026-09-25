"""Spec compliance smoke tests for vCons built via the python `vcon` library.

These tests are the safety net for adapters built from this template. Keep them
green. If you add a new field to your adapter's vCon construction, add a test
here that proves it conforms to the spec.
"""

from __future__ import annotations

import json

import pytest

from vcon_vac_adapter.vcon_builder import (
    VCON_SYNTAX,
    external_media_url,
    new_vcon,
    sha512_b64url,
)


def test_syntax_is_0_4_0() -> None:
    v = new_vcon()
    assert v.vcon_dict["vcon"] == "0.4.0"
    assert VCON_SYNTAX == "0.4.0"


def test_build_new_strips_group_and_redacted() -> None:
    v = new_vcon()
    assert "group" not in v.vcon_dict
    assert "redacted" not in v.vcon_dict


def test_subject_is_written_via_vcon_dict() -> None:
    v = new_vcon(subject="Test call")
    assert v.vcon_dict["subject"] == "Test call"


def test_extensions_listed_at_top_level() -> None:
    v = new_vcon(extensions=["sip-signaling", "lawful_basis"])
    assert v.vcon_dict["extensions"] == ["sip-signaling", "lawful_basis"]


def test_lib_add_attachment_uses_purpose_with_party_and_dialog() -> None:
    """vcon-lib >=0.9.2: `add_attachment` is spec-correct out of the box."""
    v = new_vcon()
    v.add_attachment(
        purpose="call_metadata",
        mediatype="application/json",
        body={"foo": "bar"},  # -04: raw JSON value under encoding "json"
        encoding="json",
        party=0,
        dialog=0,
    )
    att = v.vcon_dict["attachments"][0]
    assert att["purpose"] == "call_metadata"
    assert "type" not in att  # NEVER use legacy `type` field on attachments
    assert att["party"] == 0
    assert att["dialog"] == 0
    assert att["encoding"] == "json"
    assert att["body"] == {"foo": "bar"}


def test_lib_add_analysis_uses_schema_not_schema_version() -> None:
    v = new_vcon()
    v.add_analysis(
        type="transcript",
        dialog=0,
        vendor="openai-whisper",
        product="whisper-large-v3",
        body=json.dumps({"text": "hello"}),
        encoding="json",
        schema="https://datatracker.ietf.org/doc/draft-howe-vcon-wtf-extension/",
    )
    a = v.vcon_dict["analysis"][0]
    assert a["schema"].startswith("https://")
    assert "schema_version" not in a  # NEVER schema_version
    assert a["vendor"] == "openai-whisper"  # REQUIRED


def test_lib_add_analysis_requires_vendor() -> None:
    """The lib enforces `vendor` as a required kwarg."""
    v = new_vcon()
    with pytest.raises(TypeError):
        v.add_analysis(type="transcript", dialog=0, body="hi")  # type: ignore[call-arg]


def test_lib_add_tag_writes_party_and_dialog() -> None:
    """vcon-lib >=0.9.3 writes `party`/`dialog` on the tags attachment correctly."""
    v = new_vcon()
    v.add_tag("source", "test-platform")
    v.add_tag("session_id", "abc123")
    tags_atts = [a for a in v.vcon_dict["attachments"] if a.get("purpose") == "tags"]
    assert tags_atts, "add_tag should have produced a tags attachment"
    for att in tags_atts:
        assert att["party"] == 0
        assert att["dialog"] == 0


def test_content_hash_format() -> None:
    h = sha512_b64url(b"hello world")
    assert h.startswith("sha512-")
    # base64url has no padding, no `+/` chars
    assert "=" not in h
    assert "+" not in h
    assert "/" not in h


def test_external_media_dialog_has_url_and_content_hash() -> None:
    body = external_media_url(url="https://example.com/r.wav", content=b"x", mediatype="audio/wav")
    assert body["url"] == "https://example.com/r.wav"
    assert body["content_hash"].startswith("sha512-")
    assert body["mediatype"] == "audio/wav"


@pytest.mark.parametrize("legacy_field", ["appended", "must_support"])
def test_no_legacy_field_names_in_serialized_vcon(legacy_field: str) -> None:
    """`appended` and `must_support` are legacy vcon-mcp column names — never written out."""
    v = new_vcon()
    v.add_analysis(type="t", vendor="v", body="b", dialog=0)
    v.add_attachment(purpose="p", body=json.dumps({}), encoding="json", party=0, dialog=0)
    v.add_tag("x", "y")
    serialized = json.dumps(v.vcon_dict)
    assert legacy_field not in serialized
