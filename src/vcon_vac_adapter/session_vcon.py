"""Compose an IR Session into a final vCon per draft-howe-vcon-agent-session-00.

Builds:
  - parties[0] = user; parties[1..N] = each agent with `meta.agent_session`
  - dialog[]   = one entry per `Entry(kind="message")`
  - analysis[] = one (or per-tool-call) `agent_trace` entries embedding a VAC
                 record per draft-birkholz-verifiable-agent-conversations
  - attachments[] = `agent_file_change`, `agent_environment`, and (optionally)
                    `lawful_basis`

Uses the lib-first primitives in `vcon_builder.py` and the `vcon` library's
`add_party` / `add_dialog` / `add_analysis` / `add_attachment` to stay
spec-correct.
"""

from __future__ import annotations

import json
from dataclasses import asdict
from typing import Any, Literal

from vcon import Vcon
from vcon.dialog import Dialog
from vcon.party import Party

from .ir import FileChange, Session
from .lawful_basis import DEFAULT_PURPOSES
from .vac_builder import VAC_SCHEMA_URL, build_vac_record
from .vcon_builder import new_vcon, sha512_b64url

Granularity = Literal["session", "per_tool_call"]


def _filechange_body(fc: FileChange) -> str:
    d = {
        "path": fc.path,
        "contributor": fc.contributor or fc.agent_id,
        "operation": fc.operation,
        "line_range": list(fc.line_range) if fc.line_range else None,
        "commit": fc.commit,
        "content_hash": fc.content_hash,
        "diff_text": fc.diff_text,
    }
    return json.dumps(
        {k: v for k, v in d.items() if v is not None}, sort_keys=True, separators=(",", ":")
    )


def _agent_meta(session: Session, agent_id: str) -> dict[str, Any]:
    agent = next(a for a in session.agents if a.agent_id == agent_id)
    env = {k: v for k, v in asdict(agent.environment).items() if v is not None}
    meta: dict[str, Any] = {
        "agent_session": {
            "model_id": agent.model_id,
            "provider": agent.provider,
            "recording_agent": agent.recording_agent,
            "agent_id": agent.agent_id,
        }
    }
    if agent.parent_agent_id:
        meta["agent_session"]["parent_agent_id"] = agent.parent_agent_id
    if env:
        meta["agent_session"]["environment"] = env
    return meta


def _dialog_for_entry(
    dialog_idx: dict[str, int],
    entry_id: str,
    fallback: int = 0,
) -> int:
    return dialog_idx.get(entry_id, fallback)


def build_vcon(
    session: Session,
    *,
    granularity: Granularity = "session",
    include_lawful_basis: bool = True,
    include_environment_attachments: bool = True,
    vac_encoding: Literal["json", "cbor"] = "json",
    critical_agent_session: bool = False,
) -> Vcon:
    extensions: list[str] = ["agent_session"]
    if include_lawful_basis and session.lawful_basis is not None:
        extensions.append("lawful_basis")
    v = new_vcon(extensions=extensions)

    if critical_agent_session:
        v.vcon_dict["critical"] = ["agent_session"]

    # --- 1. parties ---
    user_party = dict(session.user_party) if session.user_party else {"role": "user"}
    user_party.setdefault("role", "user")
    v.add_party(Party(**user_party))

    agent_index: dict[str, int] = {}
    agent_validation = "synthetic" if user_party.get("validation") == "synthetic" else "system"
    for i, a in enumerate(session.agents, start=1):
        v.add_party(
            Party(
                name=a.name or a.model_id,
                role="agent",
                validation=agent_validation,
                meta=_agent_meta(session, a.agent_id),
            )
        )
        agent_index[a.agent_id] = i

    # --- 2. dialog: one entry per message turn ---
    dialog_idx_for_entry: dict[str, int] = {}
    for e in session.entries:
        if e.kind != "message":
            continue
        if e.role == "user":
            originator = 0
            parties = [0]
        else:
            ai = agent_index.get(e.agent_id, 1)
            originator = ai
            parties = [0, ai]
        v.add_dialog(
            Dialog(
                type="text",
                start=e.timestamp,
                parties=parties,
                originator=originator,
                body=e.text or "",
                encoding="none",
                mediatype="text/plain",
            )
        )
        dialog_idx_for_entry[e.entry_id] = len(v.vcon_dict["dialog"]) - 1

    all_dialog_indices = sorted(dialog_idx_for_entry.values())

    # --- 3. analysis: VAC agent_trace ---
    def _emit_trace(sess: Session, dialog: list[int]) -> None:
        enc, body = build_vac_record(sess, cbor=(vac_encoding == "cbor"))
        kwargs = dict(
            type="agent_trace",
            vendor="vcon-vac-adapter",
            product=sess.source_platform,
            schema=VAC_SCHEMA_URL,
            encoding=enc,
            body=body,
            dialog=dialog,
        )
        if enc == "base64url":
            kwargs["mediatype"] = "application/cbor"
        v.add_analysis(**kwargs)

    if granularity == "session":
        _emit_trace(session, all_dialog_indices)
    else:  # per_tool_call
        for e in session.entries:
            if e.kind != "tool_call":
                continue
            sub = Session(
                session_id=session.session_id,
                started_at=e.timestamp,
                ended_at=e.timestamp,
                user_party=session.user_party,
                agents=session.agents,
                entries=[e],
                file_changes=[],
                source_platform=session.source_platform,
                source_protocol_version=session.source_protocol_version,
                raw_meta=session.raw_meta,
                lawful_basis=None,
            )
            parent_dialog = _dialog_for_entry(dialog_idx_for_entry, e.parent_id or "", 0)
            _emit_trace(sub, [parent_dialog])

    # --- 4. attachments: file_changes, environment, lawful_basis ---
    for fc in session.file_changes:
        body = _filechange_body(fc)
        v.add_attachment(
            purpose="agent_file_change",
            party=agent_index.get(fc.agent_id, 1),
            dialog=_dialog_for_entry(dialog_idx_for_entry, fc.entry_id, 0),
            encoding="json",
            body=body,
            content_hash=sha512_b64url(body.encode("utf-8")),
        )

    if include_environment_attachments:
        for a in session.agents:
            env = {k: v_ for k, v_ in asdict(a.environment).items() if v_ is not None}
            if not env:
                continue
            body = json.dumps(env, sort_keys=True, separators=(",", ":"))
            v.add_attachment(
                purpose="agent_environment",
                party=agent_index[a.agent_id],
                dialog=0,
                encoding="json",
                body=body,
                content_hash=sha512_b64url(body.encode("utf-8")),
            )

    if include_lawful_basis and session.lawful_basis is not None:
        body = json.dumps(session.lawful_basis, sort_keys=True, separators=(",", ":"))
        # lawful_basis is the documented exception: uses `type` not `purpose`.
        # The vcon library's add_attachment writes `purpose`; we append the dict
        # directly per the lawful_basis extension draft.
        v.vcon_dict.setdefault("attachments", []).append(
            {
                "type": "lawful_basis",
                "party": 0,
                "dialog": 0,
                "encoding": "json",
                "body": body,
                "content_hash": sha512_b64url(body.encode("utf-8")),
            }
        )

    # Mark all purpose_grants present so consumers can discover scope quickly
    # without parsing the body.
    if include_lawful_basis and session.lawful_basis is not None:
        purposes = [pg.get("purpose") for pg in session.lawful_basis.get("purpose_grants", [])]
        if not purposes:
            purposes = list(DEFAULT_PURPOSES)
        v.vcon_dict.setdefault("meta", {})
        v.vcon_dict["meta"]["lawful_basis_purposes"] = purposes

    return v
