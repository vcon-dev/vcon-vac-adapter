# vcon-vac-adapter

> Convert AI-agent session transcripts (Claude Code, Anthropic Messages API, OpenAI Responses, OpenAI Agents SDK) into [vCons](https://datatracker.ietf.org/doc/draft-ietf-vcon-vcon-core/) with the `agent_session` extension, embedding a [Verifiable Agent Conversations](https://datatracker.ietf.org/doc/draft-birkholz-verifiable-agent-conversations/) (VAC) record in `analysis[]`.

**Spec targets:**
- vCon core: `draft-ietf-vcon-vcon-core-02` (syntax `"0.4.0"`)
- Agent session: [`draft-howe-vcon-agent-session-00`](https://datatracker.ietf.org/doc/draft-howe-vcon-agent-session/)
- VAC record: [`draft-birkholz-verifiable-agent-conversations`](https://datatracker.ietf.org/doc/draft-birkholz-verifiable-agent-conversations/)

[![Python 3.12+](https://img.shields.io/badge/python-3.12+-blue.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)

---

## What it does

For each AI-agent session, it produces a single vCon that carries:

1. **Parties** — the user (party 0) and each agent (party 1+) with `meta.agent_session` (model_id, provider, recording_agent, cwd, vcs_branch, vcs_commit, parent_agent_id for sub-agents).
2. **Dialog turns** — user prompts and assistant replies as ordinary `dialog[]` entries.
3. **Internal trace** — tool calls, tool results, reasoning, and system events embedded as a JSON-encoded VAC `verifiable-agent-record` in `analysis[]` with `type: "agent_trace"` and `schema` pointing at the VAC datatracker URL.
4. **File-edit provenance** — `attachments[]` entries with `purpose: "agent_file_change"` whenever the agent's tool calls touched a file (Claude Code: `Write`, `Edit`, `MultiEdit`, `NotebookEdit`, and file-touching `Bash` commands).
5. **Environment metadata** — `purpose: "agent_environment"` per agent.
6. **Optional lawful basis** — `lawful_basis` extension attachment for synthetic / test fixtures and recorded consent.

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
- `--no-lawful-basis` — skip the lawful_basis attachment.

### Daemon

```bash
cp config.example.yaml config.yaml
# edit config.yaml: source.platform, source.claude_code.projects_dir, webhook.url, ...
vac-adapter daemon
```

Daemon mode tails the configured source path, builds a vCon per detected session, and POSTs to the configured webhook with HMAC-SHA256 body signing, exponential backoff, and a dead-letter queue on full failure.

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

## Known limitations

- **Reasoning fidelity** — when ingesting via `vcon-mcp-adapters` the upstream Claude Code parser records only `thinking_block_count`; our native Claude Code parser preserves full thinking text.
- **Edit `content_hash`** is post-hoc lossy unless `--git-blame-mode` (planned) reads the file at the recorded commit via `git show`.
- **Sub-agent detection** for Claude Code relies on the Task tool naming; structural changes upstream will silently degrade to single-agent emission.

## Related

- `vcon-adapter-template` — scaffold this adapter was forked from
- `vcon-mcp-adapters` — sibling repo providing Anthropic / OpenAI / Agents SDK parsers
- `draft-kuehlewind-audit-architecture-00` — wider agent auditing architecture this adapter participates in

## License

MIT
