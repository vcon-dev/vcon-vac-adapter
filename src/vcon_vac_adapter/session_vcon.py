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

from .ir import Entry, FileChange, Session
from .vac_builder import VAC_SCHEMA_URL, build_vac_record
from .vcon_builder import LawfulBasisConfig, add_lawful_basis, new_vcon, sha512_b64url

Granularity = Literal["session", "per_tool_call"]


def _filechange_body(fc: FileChange) -> dict[str, Any]:
    """The `agent_file_change` attachment body: a raw JSON value, per
    draft-ietf-vcon-vcon-core-04 §2.3.2 (`encoding: "json"` means `body` IS
    the value, not a `json.dumps()` string).
    """
    d = {
        "path": fc.path,
        "contributor": fc.contributor or fc.agent_id,
        "operation": fc.operation,
        "line_range": list(fc.line_range) if fc.line_range else None,
        "commit": fc.commit,
        "content_hash": fc.content_hash,
        "diff_text": fc.diff_text,
    }
    return {k: v for k, v in d.items() if v is not None}


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
    lawful_basis_cfg: LawfulBasisConfig | None = None,
    include_environment_attachments: bool = True,
    vac_encoding: Literal["json", "cbor"] = "json",
    critical_agent_session: bool = False,
) -> Vcon:
    """Build the vCon. `lawful_basis_cfg` (default `LawfulBasisConfig.from_env()`)
    drives the optional `lawful_basis` attachment/extension (see
    `vcon_builder.add_lawful_basis`); `include_lawful_basis=False` skips it
    outright regardless of config. Never invent a basis: an unset
    `cfg.lawful_basis` means no attachment is added, and `add_lawful_basis`
    logs a warning rather than defaulting one.
    """
    v = new_vcon(extensions=["agent_session"])

    if critical_agent_session:
        v.vcon_dict["critical"] = ["agent_session"]

    # --- 1. parties ---
    user_party = dict(session.user_party) if session.user_party else {"role": "user"}
    user_party.setdefault("role", "user")
    v.add_party(Party(**user_party))

    agent_index: dict[str, int] = {}
    agent_validation = "synthetic" if user_party.get("validation") == "synthetic" else "system"
    for i, a in enumerate(session.agents, start=1):
        # draft-ietf-vcon-vcon-core-04 §4.2.11: every AgentRef in the IR is an
        # automated party (the primary model, a sub-agent, or a bridged
        # tool/service) — never a human — so `type: "bot"` is unconditional
        # here. §4.2.12 `org`: only set when the source actually names the
        # model vendor (AgentRef.provider is always source-derived: an OTel
        # `gen_ai.provider.name`/`gen_ai.system` attribute, or which
        # vendor-specific API/SDK a source module talked to) — never invented.
        # "unknown" (otel.py's fallback when no such attribute is present) is
        # not a real identifier, so it's left off.
        org = a.provider if a.provider and a.provider != "unknown" else None
        v.add_party(
            Party(
                name=a.name or a.model_id,
                role="agent",
                type="bot",
                org=org,
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

    # --- 3. attachments: file_changes, environment ---
    #
    # Built *before* the analysis section (4) so that each `agent_trace`
    # analysis entry can carry a correct `attachment` index/index-list
    # (draft-ietf-vcon-vcon-core-04 §4.5.3: "Index/indices of attachment
    # objects this analysis is based on") pointing at the attachments whose
    # data it embeds, rather than a hardcoded/guessed position.
    #
    # `lawful_basis` (step 5) is added *after* analysis, at the very end,
    # since no `agent_trace` analysis is ever derived from it — appending it
    # last never shifts the indices resolved here. The same holds if a
    # caller appends `lawful_basis` itself, later, via `add_lawful_basis()`
    # (e.g. a CLI `finalize` step) instead of through `include_lawful_basis`:
    # as long as it lands after every attachment an analysis might reference,
    # already-resolved indices stay valid. See
    # `test_lawful_basis_appended_after_build_does_not_shift_analysis_attachment_indices`.
    entry_timestamp = {e.entry_id: e.timestamp.isoformat() for e in session.entries}
    session_start = session.started_at.isoformat()

    # entry_id -> indices into v.vcon_dict["attachments"] of the FileChange
    # attachment(s) derived from that entry (an entry, e.g. MultiEdit, can
    # produce more than one FileChange).
    file_change_attachment_idx: dict[str, list[int]] = {}
    for fc in session.file_changes:
        body = _filechange_body(fc)
        canonical_bytes = json.dumps(body, sort_keys=True, separators=(",", ":")).encode("utf-8")
        v.add_attachment(
            purpose="agent_file_change",
            party=agent_index.get(fc.agent_id, 1),
            dialog=_dialog_for_entry(dialog_idx_for_entry, fc.entry_id, 0),
            start=entry_timestamp.get(fc.entry_id, session_start),
            encoding="json",
            mediatype="application/json",
            body=body,
            content_hash=sha512_b64url(canonical_bytes),
        )
        idx = len(v.vcon_dict["attachments"]) - 1
        file_change_attachment_idx.setdefault(fc.entry_id, []).append(idx)

    # Indices of the `agent_environment` attachments. Every `agent_trace`
    # analysis (session or per-tool-call granularity) embeds the *full*
    # `session.agents` list, environments included (see `_agent_to_vac()` /
    # `build_vac_record()`), so every trace is derived from all of these,
    # regardless of which entries it covers.
    environment_attachment_indices: list[int] = []
    if include_environment_attachments:
        for a in session.agents:
            env = {k: v_ for k, v_ in asdict(a.environment).items() if v_ is not None}
            if not env:
                continue
            body = env
            canonical_bytes = json.dumps(body, sort_keys=True, separators=(",", ":")).encode(
                "utf-8"
            )
            v.add_attachment(
                purpose="agent_environment",
                party=agent_index[a.agent_id],
                dialog=0,
                start=session_start,
                encoding="json",
                mediatype="application/json",
                body=body,
                content_hash=sha512_b64url(canonical_bytes),
            )
            environment_attachment_indices.append(len(v.vcon_dict["attachments"]) - 1)

    def _attachment_indices_for(entries: list[Entry]) -> int | list[int] | None:
        """Attachments a trace over `entries` is derived from, or `None` if
        it is derived from no attachments at all (in which case `attachment`
        is omitted — draft-ietf-vcon-vcon-core-04 §4.5.3 makes it optional
        exactly in that case).
        """
        entry_ids = {e.entry_id for e in entries}
        idxs = set(environment_attachment_indices)
        for entry_id in entry_ids:
            idxs.update(file_change_attachment_idx.get(entry_id, []))
        if not idxs:
            return None
        ordered = sorted(idxs)
        return ordered[0] if len(ordered) == 1 else ordered

    # --- 4. analysis: VAC agent_trace ---
    def _emit_trace(sess: Session, dialog: list[int]) -> None:
        # `body` is the raw VAC record dict for `encoding: "json"`, or a
        # base64url CBOR string for `encoding: "base64url"` — see
        # build_vac_record()'s docstring (draft-ietf-vcon-vcon-core-04 §2.3.2).
        enc, body = build_vac_record(sess, cbor=(vac_encoding == "cbor"))
        kwargs = dict(
            type="agent_trace",
            vendor="vcon-vac-adapter",
            product=sess.source_platform,
            schema=VAC_SCHEMA_URL,
            encoding=enc,
            body=body,
            dialog=dialog,
            mediatype="application/cbor" if enc == "base64url" else "application/json",
        )
        v.add_analysis(**kwargs)
        # `attachment` is set directly on the freshly-appended dict rather
        # than passed as an add_analysis() kwarg: vcon-lib 0.9.x's
        # `_ALLOWED_ANALYSIS_PROPERTIES` does not include `attachment` (the
        # vendored core schema, tests/schema/vcon_json_schema.json, already
        # does — see its `Analysis.attachment` property), so relying on the
        # library's non-standard-property passthrough would be fragile
        # across property_handling modes/versions.
        attachment_ids = _attachment_indices_for(sess.entries)
        if attachment_ids is not None:
            v.vcon_dict["analysis"][-1]["attachment"] = attachment_ids

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
            )
            parent_dialog = _dialog_for_entry(dialog_idx_for_entry, e.parent_id or "", 0)
            _emit_trace(sub, [parent_dialog])

    # --- 5. lawful_basis attachment (always last; see note in step 3) ---
    if include_lawful_basis:
        cfg = lawful_basis_cfg if lawful_basis_cfg is not None else LawfulBasisConfig.from_env()
        add_lawful_basis(v, cfg, granted_at=session.started_at.isoformat(), party=0, dialog=0)

    return v
