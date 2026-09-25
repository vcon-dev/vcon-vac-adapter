"""Build a Verifiable Agent Conversations (VAC) record from an IR Session.

Output is a JSON-stringified (or CBOR+base64url-encoded) `verifiable-agent-record`
ready to drop into a vCon `analysis[]` entry body per
draft-howe-vcon-agent-session-00 §6.

The upstream CDDL in draft-birkholz-verifiable-agent-conversations is still a
scaffold, so all VAC field names are kept as constants here. Validation is
best-effort runtime assertion. When the upstream schema stabilizes, change the
constants and the `_entry_to_vac` / `_agent_to_vac` projections.
"""

from __future__ import annotations

import uuid
from base64 import urlsafe_b64encode
from dataclasses import asdict
from typing import Any, Literal

from .ir import AgentRef, Entry, Session

VAC_SCHEMA_URL = "https://datatracker.ietf.org/doc/draft-birkholz-verifiable-agent-conversations/"
AGENT_SESSION_SCHEMA_URL = "https://datatracker.ietf.org/doc/draft-howe-vcon-agent-session/"
VAC_RECORD_VERSION = "0.1"
VAC_RECORD_NAMESPACE = uuid.UUID("6f0b1e1c-3a0a-5e3a-9f0b-1e1c3a0a5e3a")  # static


def session_namespace(session: Session) -> uuid.UUID:
    """Deterministic UUIDv5 namespace per session. Reruns produce the same id."""
    return uuid.uuid5(VAC_RECORD_NAMESPACE, f"vac:{session.session_id}")


def vac_entry_id(session: Session, entry: Entry) -> str:
    """Stable per (session_id, entry.entry_id) so reruns produce identical ids."""
    return str(uuid.uuid5(session_namespace(session), entry.entry_id))


def _agent_to_vac(agent: AgentRef) -> dict[str, Any]:
    return {
        "agent-id": agent.agent_id,
        "name": agent.name or agent.model_id,
        "model-id": agent.model_id,
        "provider": agent.provider,
        "recording-agent": agent.recording_agent,
        "parent-agent-id": agent.parent_agent_id,
        "environment": {k: v for k, v in asdict(agent.environment).items() if v is not None},
    }


def _entry_to_vac(session: Session, entry: Entry) -> dict[str, Any]:
    base: dict[str, Any] = {
        "entry-id": vac_entry_id(session, entry),
        "kind": entry.kind,
        "timestamp": entry.timestamp.isoformat(),
        "agent-id": entry.agent_id,
    }
    if entry.parent_id is not None:
        base["parent-id"] = vac_entry_id(session, _stub_entry_for_lookup(session, entry.parent_id))
    if entry.kind == "message":
        base["role"] = entry.role
        base["text"] = entry.text
    elif entry.kind == "tool_call":
        base["tool-name"] = entry.tool_name
        base["tool-use-id"] = entry.tool_use_id
        base["tool-input"] = entry.tool_input
    elif entry.kind == "tool_result":
        base["tool-use-id"] = entry.tool_use_id
        base["tool-output"] = entry.tool_output
        if entry.is_error is not None:
            base["is-error"] = entry.is_error
        if entry.duration_ms is not None:
            base["duration-ms"] = entry.duration_ms
    elif entry.kind == "reasoning":
        base["text"] = entry.text
    elif entry.kind == "event":
        base["event-type"] = entry.event_type
    if entry.meta:
        base["meta"] = entry.meta
    return base


def _stub_entry_for_lookup(session: Session, entry_id: str) -> Entry:
    """Find the entry that `parent_id` references so we can re-derive its UUID."""
    for e in session.entries:
        if e.entry_id == entry_id:
            return e
    # If parent isn't in the session, return a stub with just the id (still
    # produces a deterministic UUIDv5 — consumer can still see it).
    import datetime as _dt

    from .ir import Entry as _E

    return _E(
        entry_id=entry_id, kind="event", timestamp=_dt.datetime.fromtimestamp(0), agent_id="unknown"
    )


def _validate_record(record: dict[str, Any]) -> None:
    """Best-effort structural assertion — surface obvious shape bugs early.

    Tight CDDL-driven validation lands when the upstream draft stabilizes.
    """
    var = record["verifiable-agent-record"]
    assert var["version"] == VAC_RECORD_VERSION
    trace = var["session-trace"]
    entries = trace["entries"]
    assert isinstance(entries, list)
    for e in entries:
        assert "entry-id" in e
        assert "kind" in e
        assert "timestamp" in e


def build_vac_record(
    session: Session, *, cbor: bool = False
) -> tuple[Literal["json", "base64url"], Any]:
    """Return (encoding, body) tuple.

    Per draft-ietf-vcon-vcon-core-04 §2.3.2: for `encoding: "json"`, `body` is
    the raw JSON value (a dict here, not a `json.dumps()` string). For
    `encoding: "base64url"` (CBOR mode), `body` is the base64url-encoded
    string as before.
    """
    record = {
        "verifiable-agent-record": {
            "version": VAC_RECORD_VERSION,
            "session-trace": {
                "session-id": str(session_namespace(session)),
                "started-at": session.started_at.isoformat(),
                "ended-at": session.ended_at.isoformat() if session.ended_at else None,
                "source-platform": session.source_platform,
                "entries": [_entry_to_vac(session, e) for e in session.entries],
            },
            "agents": [_agent_to_vac(a) for a in session.agents],
        }
    }
    _validate_record(record)
    if cbor:
        import cbor2  # type: ignore[import-not-found]

        return "base64url", urlsafe_b64encode(cbor2.dumps(record)).rstrip(b"=").decode("ascii")
    return "json", record
