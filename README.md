# vcon-vac-adapter

> Convert AI-agent session transcripts (Claude Code, Anthropic Messages API, OpenAI Responses, OpenAI Agents SDK) into [vCons](https://datatracker.ietf.org/doc/draft-ietf-vcon-vcon-core/) with the `agent_session` extension, embedding a [Verifiable Agent Conversations](https://datatracker.ietf.org/doc/draft-birkholz-verifiable-agent-conversations/) (VAC) record in `analysis[]`.

**Spec targets:**
- vCon core: `draft-ietf-vcon-vcon-core-04` (syntax `"0.4.0"`)
- Agent session: [`draft-howe-vcon-agent-session-00`](https://datatracker.ietf.org/doc/draft-howe-vcon-agent-session/)
- VAC record: [`draft-birkholz-verifiable-agent-conversations`](https://datatracker.ietf.org/doc/draft-birkholz-verifiable-agent-conversations/)

[![Python 3.12+](https://img.shields.io/badge/python-3.12+-blue.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)

---

## What it does

For each AI-agent session, it produces a single vCon that carries:

1. **Parties** — the user (party 0) and each agent (party 1+) with `meta.agent_session` (model_id, provider, recording_agent, cwd, vcs_branch, vcs_commit, parent_agent_id for sub-agents).
   - **Party `type` (§4.2.11):** every agent party — the primary model, a Claude Code sub-agent detected from a `Task` tool call, or a bridged tool/service — is always automated, so it always gets `type: "bot"`. The user party gets `type: "person"` only when the source makes that unambiguous: a Claude Code session's own `"user"` JSONL events, or a direct Anthropic Messages API / OpenAI Responses API `role: "user"` turn. It is left off for OpenAI Agents SDK sessions (a multi-agent orchestration runtime where the top-level "user" turn can itself be another agent or an upstream service) and for OTel GenAI spans (no role guarantee at all) rather than guessed.
   - **Party `org` (§4.2.12):** set on a bot party to `AgentRef.provider` when that provider is source-derived and not the `"unknown"` fallback — an OTel `gen_ai.provider.name`/`gen_ai.system` attribute, or which vendor-specific API/SDK a source module talked to (Claude Code and the Anthropic Messages API bridge → `anthropic`; the OpenAI Responses/Agents bridges → `openai`). Never invented. `dept` (§4.2.13) is not populated anywhere — no source here carries a department identifier.
2. **Dialog turns** — user prompts and assistant replies as ordinary `dialog[]` entries.
3. **Internal trace** — tool calls, tool results, reasoning, and system events embedded as a JSON-encoded VAC `verifiable-agent-record` in `analysis[]` with `type: "agent_trace"` and `schema` pointing at the VAC datatracker URL.
4. **File-edit provenance** — `attachments[]` entries with `purpose: "agent_file_change"` whenever the agent's tool calls touched a file (Claude Code: `Write`, `Edit`, `MultiEdit`, `NotebookEdit`, and file-touching `Bash` commands).
5. **Environment metadata** — `purpose: "agent_environment"` per agent.
6. **Optional lawful basis** — `purpose: "lawful_basis"` attachment (never the legacy `type` field) driven by `LAWFUL_BASIS`/`LAWFUL_BASIS_*` env vars or a `vcon.lawful_basis:` config block (see "Lawful basis" below). Never defaulted: an unset basis means no attachment, and a one-time warning is logged.

## v0.1 platform support

| Platform | Mode | Notes |
|---|---|---|
| Claude Code | file (`~/.claude/projects/**/*.jsonl`) | Native parser; sub-agents via Task tool |
| Anthropic Messages API | request/response logs | via `vcon-mcp-adapters` bridge |
| OpenAI Responses API | request/response logs | via `vcon-mcp-adapters` bridge |
| OpenAI Agents SDK | live `TracingProcessor` | enqueue-only callback; daemon consumes |
| OpenTelemetry | OTLP/JSON spans | GenAI semantic conventions; no vendor SDK, no extra deps |

Anthropic/OpenAI platforms require `pip install vcon-mcp-adapters` alongside this adapter.

## Install

```bash
uv pip install -e .
# Optional extras:
uv pip install -e ".[cbor,signing,dev]"
# For Anthropic / OpenAI source bridges:
uv pip install vcon-mcp-adapters
```

## Use

### One-shot CLI

```bash
vac-adapter convert \
  ~/.claude/projects/-Users-x-proj/2026-05-22-abc.jsonl \
  --platform claude_code \
  --out /tmp/session.vcon.json
```

### OTel spans in, signed vCon out

Point it at anything that already emits the OpenTelemetry GenAI semantic
conventions — an OTLP/JSON export body, or a bare JSON array of spans:

```bash
vac-adapter convert trace.json --platform otel --sign-key private.pem --out session.vcon.json
```

Spans map by `gen_ai.operation.name`: `chat` / `text_completion` /
`generate_content` / `invoke_agent` become `dialog[]` turns (from
`gen_ai.input.messages` and `gen_ai.output.messages`, or the legacy
`gen_ai.user.message` / `gen_ai.choice` span events); `execute_tool` becomes a
`tool_call` + `tool_result` pair inside the VAC record; everything else under
the trace becomes a VAC `event`. Model, provider, and agent identity come from
`gen_ai.provider.name` / `gen_ai.request.model` / `gen_ai.agent.*`; host and OS
from resource attributes. A tool span with no model attributes inherits its
parent span's agent.

`--sign-key` JWS-signs the finished vCon (RSA private key, PEM) so the output
is a verifiable artifact, not just a JSON blob.

Options:
- `--granularity {session,per_tool_call}` — single `agent_trace` per session (default) vs one per tool call (for selective redaction).
- `--vac-encoding {json,cbor}` — CBOR mode emits a base64url-encoded CBOR record with `mediatype: application/cbor`.
- `--critical-agent-session` — also add `agent_session` to vCon `critical[]`.
- `--no-lawful-basis` — skip the lawful_basis attachment outright, regardless of `LAWFUL_BASIS`.
- `--post` — also deliver the finished vCon. Needs either `--webhook-url` (+ optional `--webhook-secret` for HMAC signing) or `--conserver-url` (+ optional `--conserver-token`, repeatable `--ingress-list`) to POST directly to a vcon-server `/vcon` endpoint.

```bash
LAWFUL_BASIS=consent LAWFUL_BASIS_PURPOSE=recording \
vac-adapter convert session.jsonl --platform claude_code --out session.vcon.json \
  --post --conserver-url https://conserver.example.com --conserver-token "$CONSERVER_TOKEN"
```

### Lawful basis

Never defaulted in code. Set via env vars (checked by `vcon_builder.LawfulBasisConfig.from_env()`,
used by `convert` and by daemon mode when no YAML `vcon.lawful_basis:` block overrides it):

- `LAWFUL_BASIS` — one of `consent`, `contract`, `legal_obligation`, `vital_interests`, `public_task`, `legitimate_interests`. Unset means no `lawful_basis` attachment is added, and a warning is logged once per process.
- `LAWFUL_BASIS_PURPOSE` — comma-separated purpose grants (default `recording`).
- `LAWFUL_BASIS_JURISDICTION`, `LAWFUL_BASIS_EXPIRATION` (ISO 8601), `LAWFUL_BASIS_PROOF_MECHANISM`, `LAWFUL_BASIS_PROOF_DESCRIPTION` — optional.

Or, in `config.yaml`, under `vcon.lawful_basis:` (env vars still win per-field when both are set — see `LawfulBasisConfig.resolve()`):

```yaml
vcon:
  lawful_basis:
    lawful_basis: consent
    purposes: [recording, transcription]
    jurisdiction: US-MA
```

The resulting attachment uses `purpose: "lawful_basis"` (never the legacy `type` field), a string
JSON `body`, and `mediatype: "application/json"`; `"lawful_basis"` is added to the vCon's top-level
`extensions[]`.

### Daemon

```bash
cp config.example.yaml config.yaml
# edit config.yaml: adapter.source_platform, source.claude_code.watch_dir, webhook.endpoints, ...
vac-adapter daemon
```

When `adapter.source_platform: claude_code` and `source.claude_code.watch_dir` are both set, daemon
mode watches that directory (via `watchfiles`) for Claude Code session `.jsonl` files, builds a vCon
per new-or-changed session, and delivers it (webhook, HMAC-SHA256-signed, or conserver-direct per
`delivery.mode`) with exponential backoff and a dead-letter queue on full failure. Delivery is
idempotent per file content hash, tracked in `.vac-adapter-watch-state.json` inside the watched
directory, so restarting the daemon doesn't redeliver unchanged sessions.

**`source.claude_code.watch_dir` is never defaulted to the real `~/.claude/projects`.** It must be
set explicitly (typically via `${SOME_ENV_VAR}` substitution) — an operator who wants to watch their
own Claude Code projects directory opts in by pointing this at it themselves. With no `watch_dir`
configured, or a `source_platform` other than `claude_code`, the daemon just serves `/healthz` and
`/metrics` until stopped; other platforms (Anthropic, OpenAI Responses, OpenAI Agents SDK, OTel) have
no watcher yet — use one-shot `convert --post` for those, or watch their log/trace output yourself
and shell out to `convert`.

`/healthz` and Prometheus `/metrics` are exposed on `server.host:server.port`.

## Spec compliance

The adapter inherits the 14 spec-compliance smoke tests from `vcon-adapter-template` plus 14 additional `agent_session` / VAC assertions:

- `vcon: "0.4.0"`
- `agent_session` in `extensions[]`
- Every agent party carries `meta.agent_session.{model_id, provider, recording_agent}`
- `analysis.type == "agent_trace"` with required `vendor`, `schema`, valid JSON `body` parsable as VAC
- Deterministic VAC entry IDs (UUIDv5 from session namespace)
- `agent_file_change` attachments include `purpose`, `party`, `dialog`, `content_hash`
- Per-tool-call granularity emits one trace per `tool_call`
- CBOR mode round-trips canonically

Run the suite:

```bash
pytest
```

## Claude Code parser: two implementations, on purpose

This repo's `sources/claude_code.py` and `vcon-mcp-adapters`' `adapters/claude_code.py` both parse
the same Claude Code JSONL session format, and are not interchangeable — they target different
output schemas for different purposes, so this adapter keeps its own parser canonical rather than
delegating to the sibling repo's:

- **This repo's parser** builds the local `ir.Session` IR: multi-agent parties (sub-agents detected
  via `Task` tool calls with a `subagent_type`), a parent/child entry tree, full `reasoning` block
  text, and derived `agent_file_change` records. `session_vcon.build_vcon` needs all of that to emit
  the `agent_session` extension's multi-party structure and the VAC `agent_trace` record — the two
  things this adapter exists to produce.
- **`vcon-mcp-adapters`' parser** builds an `MCPSession`: single fixed user/assistant party pair (MCP
  tool providers get their own party by naming convention only), no sub-agent detection, and only a
  `thinking_block_count` in place of reasoning text — right-sized for its own single-agent redaction
  and analytics use cases, not for this adapter's multi-agent VAC record.

Recommendation: keep both. Neither should delegate to the other — collapsing them would either
strip multi-agent/reasoning fidelity from this adapter's vCons, or add IR-specific complexity
(sub-agent tracking, entry trees) that `vcon-mcp-adapters`' other consumers don't need. What's worth
sharing instead is the low-level JSONL event-shape knowledge (block types, tool_use/tool_result
pairing, sub-agent Task-call detection) if it ever drifts between the two — currently duplicated by
necessity, not by oversight.

## Known limitations

- **Reasoning fidelity** — when ingesting via `vcon-mcp-adapters` the upstream Claude Code parser records only `thinking_block_count`; our native Claude Code parser preserves full thinking text. See "Claude Code parser" above for why the two parsers aren't merged.
- **Edit `content_hash`** is post-hoc lossy unless `--git-blame-mode` (planned) reads the file at the recorded commit via `git show`.
- **Sub-agent detection** for Claude Code relies on the Task tool naming; structural changes upstream will silently degrade to single-agent emission.
- **Daemon mode** only watches Claude Code session directories so far (see "Daemon" above); other source platforms need `convert --post` driven externally.

## Related

- `vcon-adapter-template` — scaffold this adapter was forked from
- `vcon-mcp-adapters` — sibling repo providing Anthropic / OpenAI / Agents SDK parsers
- `draft-kuehlewind-audit-architecture-00` — wider agent auditing architecture this adapter participates in

## License

MIT
