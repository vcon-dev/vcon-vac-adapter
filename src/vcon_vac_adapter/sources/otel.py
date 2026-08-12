"""OpenTelemetry GenAI spans → IR Session.

Consumes OTLP/JSON (`ExportTraceServiceRequest`, i.e. what an OTLP HTTP
exporter POSTs to `/v1/traces`) or a bare list of span dicts, and projects the
GenAI semantic conventions onto the IR:

    gen_ai.operation.name = chat|text_completion|generate_content|invoke_agent
        → message entries from gen_ai.input.messages / gen_ai.output.messages
          (or the legacy gen_ai.*.message / gen_ai.choice span events)
    gen_ai.operation.name = execute_tool
        → tool_call (+ tool_result) entries
    anything else under a GenAI trace
        → event entry

Reuse, not reinvention: the span is the substrate, VAC + agent_session
projection is already done downstream in vac_builder / session_vcon.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any, Literal, cast

from ..ir import AgentEnv, AgentRef, Entry, Session

# ponytail: attribute names only; no OTel SDK dependency, the payload is JSON.
OP = "gen_ai.operation.name"
CHAT_OPS = {"chat", "text_completion", "generate_content", "invoke_agent"}
LEGACY_EVENT_ROLE = {
    "gen_ai.system.message": "user",  # system prompt has no vCon role; fold to user
    "gen_ai.user.message": "user",
    "gen_ai.assistant.message": "assistant",
    "gen_ai.choice": "assistant",
}


def _anyvalue(v: Any) -> Any:
    """Unwrap an OTLP AnyValue; pass through plain JSON values."""
    if not isinstance(v, dict):
        return v
    for k in ("stringValue", "string_value", "boolValue", "bool_value"):
        if k in v:
            return v[k]
    for k in ("intValue", "int_value"):
        if k in v:
            return int(v[k])
    for k in ("doubleValue", "double_value"):
        if k in v:
            return float(v[k])
    for k in ("arrayValue", "array_value"):
        if k in v:
            return [_anyvalue(x) for x in v[k].get("values", [])]
    for k in ("kvlistValue", "kvlist_value"):
        if k in v:
            return _attrs(v[k].get("values", []))
    return v


def _attrs(raw: Any) -> dict[str, Any]:
    """Normalize OTLP KeyValue lists and plain dicts to a flat dict."""
    if isinstance(raw, dict):
        return {k: _anyvalue(v) for k, v in raw.items()}
    return {kv["key"]: _anyvalue(kv.get("value")) for kv in (raw or []) if "key" in kv}


def _ts(value: Any) -> datetime:
    """Unix nanos (int or string, as OTLP/JSON encodes them) or ISO-8601."""
    if isinstance(value, str) and not value.isdigit():
        return datetime.fromisoformat(value)
    return datetime.fromtimestamp(int(value or 0) / 1e9, tz=UTC)


def _get(d: dict[str, Any], *names: str, default: Any = None) -> Any:
    for n in names:
        if d.get(n) not in (None, ""):
            return d[n]
    return default


def _flatten(payload: Any) -> list[dict[str, Any]]:
    """Yield spans with `_resource` and `_scope` folded in."""
    if isinstance(payload, list):
        return [dict(s, _resource={}, _scope={}) for s in payload]
    out: list[dict[str, Any]] = []
    for rs in _get(payload, "resourceSpans", "resource_spans", default=[]):
        res = _attrs(_get(rs, "resource", default={}).get("attributes"))
        for ss in _get(rs, "scopeSpans", "scope_spans", default=[]):
            scope = _get(ss, "scope", default={}) or {}
            for span in _get(ss, "spans", default=[]):
                out.append(dict(span, _resource=res, _scope=scope))
    return out


def _json_maybe(value: Any) -> Any:
    if isinstance(value, str):
        try:
            return json.loads(value)
        except ValueError:
            return value
    return value


Role = Literal["user", "assistant"]


def _message_texts(msg: Any) -> list[tuple[Role, str]]:
    """(role, text) pairs from a GenAI message: {role, content} or {role, parts}."""
    if isinstance(msg, str):
        return [("assistant", msg)]
    if not isinstance(msg, dict):
        return []
    role: Role = "user" if msg.get("role") == "user" else "assistant"
    if "parts" in msg:
        out: list[tuple[Role, str]] = []
        for part in msg["parts"]:
            if isinstance(part, dict) and part.get("type") in ("text", "reasoning"):
                out.append((role, str(part.get("content", ""))))
            elif isinstance(part, str):
                out.append((role, part))
        return out
    content = msg.get("content", msg.get("message", ""))
    if isinstance(content, list):  # Anthropic-style content blocks
        return [
            (role, str(b.get("text", "")))
            for b in content
            if isinstance(b, dict) and b.get("type") == "text"
        ]
    return [(role, str(content))] if content else []


def _agent_for(attrs: dict[str, Any], resource: dict[str, Any], scope: dict[str, Any]) -> AgentRef:
    model = _get(attrs, "gen_ai.response.model", "gen_ai.request.model", default="unknown")
    provider = _get(attrs, "gen_ai.provider.name", "gen_ai.system", default="unknown")
    agent_id = _get(attrs, "gen_ai.agent.id", "gen_ai.agent.name", default=f"{provider}:{model}")
    return AgentRef(
        agent_id=str(agent_id),
        model_id=str(model),
        provider=str(provider),
        recording_agent=str(scope.get("name") or "opentelemetry"),
        name=_get(attrs, "gen_ai.agent.name"),
        environment=AgentEnv(
            host=_get(resource, "host.name"),
            os=_get(resource, "os.type"),
            cwd=_get(resource, "process.working_directory"),
        ),
    )


def parse_spans(payload: Any) -> Session:
    """Project an OTLP trace of GenAI spans into an IR Session."""
    spans = _flatten(payload)
    if not spans:
        raise ValueError("no spans in payload")
    # ponytail: parents start before children in any sane tracer, so start-time
    # order satisfies the IR's depth-first/parent-first invariant.
    spans.sort(
        key=lambda s: (
            int(_get(s, "startTimeUnixNano", "start_time_unix_nano", default=0) or 0),
            str(_get(s, "spanId", "span_id", default="")),
        )
    )

    agents: dict[str, AgentRef] = {}
    entries: list[Entry] = []
    span_agent: dict[str, str] = {}
    session_id = ""
    scope_version = ""

    for span in spans:
        attrs = _attrs(span.get("attributes"))
        span_id = str(_get(span, "spanId", "span_id", default=""))
        parent_id = str(_get(span, "parentSpanId", "parent_span_id", default="")) or None
        start, end = (
            _ts(_get(span, "startTimeUnixNano", "start_time_unix_nano")),
            _ts(_get(span, "endTimeUnixNano", "end_time_unix_nano")),
        )
        scope_version = scope_version or str(span["_scope"].get("version", ""))
        session_id = session_id or str(
            _get(attrs, "gen_ai.conversation.id", "session.id")
            or _get(span, "traceId", "trace_id", default="")
        )

        agent = _agent_for(attrs, span["_resource"], span["_scope"])
        # A tool/child span usually carries no model attrs: inherit the parent's agent.
        if agent.model_id == "unknown" and parent_id in span_agent:
            agent_id = span_agent[parent_id]
        else:
            agents.setdefault(agent.agent_id, agent)
            agent_id = agent.agent_id
        span_agent[span_id] = agent_id

        op = str(_get(attrs, OP, default=str(span.get("name", "")).split(" ")[0]))
        duration_ms = (end - start).total_seconds() * 1000

        if op == "execute_tool":
            tool_id = str(_get(attrs, "gen_ai.tool.call.id", default=span_id))
            entries.append(
                Entry(
                    entry_id=span_id,
                    kind="tool_call",
                    timestamp=start,
                    agent_id=agent_id,
                    parent_id=parent_id,
                    tool_name=str(
                        _get(attrs, "gen_ai.tool.name", default=span.get("name", "tool"))
                    ),
                    tool_use_id=tool_id,
                    tool_input=_json_maybe(_get(attrs, "gen_ai.tool.call.arguments")) or {},
                    duration_ms=duration_ms,
                )
            )
            result = _get(attrs, "gen_ai.tool.call.result")
            status = (span.get("status") or {}).get("code")
            is_error = status in (2, "STATUS_CODE_ERROR")
            if result is not None or is_error:
                entries.append(
                    Entry(
                        entry_id=f"{span_id}:result",
                        kind="tool_result",
                        timestamp=end,
                        agent_id=agent_id,
                        parent_id=span_id,
                        tool_use_id=tool_id,
                        tool_output=_json_maybe(result),
                        is_error=is_error,
                    )
                )
            continue

        if op in CHAT_OPS:
            msgs: list[tuple[Role, str, datetime]] = []
            for key, ts in (("gen_ai.input.messages", start), ("gen_ai.output.messages", end)):
                payload_msgs = _json_maybe(_get(attrs, key)) or []
                if isinstance(payload_msgs, dict):
                    payload_msgs = [payload_msgs]
                for m in payload_msgs:
                    msgs += [(r, t, ts) for r, t in _message_texts(m)]
            if not msgs:  # legacy: content rode on span events
                for ev in span.get("events", []) or []:
                    ev_role = LEGACY_EVENT_ROLE.get(str(ev.get("name")))
                    if not ev_role:
                        continue
                    ev_attrs = _attrs(ev.get("attributes"))
                    body = _json_maybe(_get(ev_attrs, "gen_ai.event.content", "content", "message"))
                    for _r, text in _message_texts(
                        body if isinstance(body, dict) else {"role": ev_role, "content": body}
                    ):
                        msgs.append(
                            (
                                cast(Role, ev_role),
                                text,
                                _ts(ev.get("timeUnixNano", ev.get("time_unix_nano", 0))),
                            )
                        )
            for i, (role, text, ts) in enumerate(msgs):
                if not text:
                    continue
                entries.append(
                    Entry(
                        entry_id=f"{span_id}:{i}",
                        kind="message",
                        timestamp=ts,
                        agent_id=agent_id,
                        parent_id=parent_id,
                        role=role,
                        text=text,
                    )
                )
            continue

        entries.append(
            Entry(
                entry_id=span_id,
                kind="event",
                timestamp=start,
                agent_id=agent_id,
                parent_id=parent_id,
                event_type=str(span.get("name", op)),
                duration_ms=duration_ms,
                meta={k: v for k, v in attrs.items() if k.startswith("gen_ai.")} or None,
            )
        )

    if not agents:  # tool-only trace: still needs a party to hang entries on
        agents["unknown"] = AgentRef(
            agent_id="unknown",
            model_id="unknown",
            provider="unknown",
            recording_agent="opentelemetry",
        )
    return Session(
        session_id=session_id or "otel-session",
        started_at=min(e.timestamp for e in entries) if entries else datetime.now(UTC),
        ended_at=max(e.timestamp for e in entries) if entries else None,
        user_party={"role": "user"},
        agents=list(agents.values()),
        entries=entries,
        source_platform="otel",
        source_protocol_version=f"otlp/{scope_version}" if scope_version else "otlp",
        raw_meta={"span_count": len(spans)},
    )
