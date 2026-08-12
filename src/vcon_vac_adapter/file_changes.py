"""Derive FileChange records from agent tool calls.

Claude Code tool name → semantics:
  Write          → create or update (we record `update` unconditionally; consumer
                   can compare against `commit` to determine create-vs-update)
  Edit           → update; line_range best-effort regex on tool_result text
  MultiEdit      → one FileChange per edit in tool_input["edits"]
  NotebookEdit   → update; line_range = (cell_index, cell_index)
  Bash           → infer from `>`, `>>`, `tee`, `cp`, `mv`, `rm`

OpenAI function_call file edits have no native naming convention; callers pass
a `tool_name_allowlist` to opt those tools in.
"""

from __future__ import annotations

import hashlib
import re
import shlex
from base64 import urlsafe_b64encode
from collections.abc import Iterable

from .ir import Entry, FileChange, Operation

_LINE_RANGE_RE = re.compile(r"L(\d+)\s*[-–]\s*L?(\d+)")  # noqa: RUF001 — en dash is intentional


def _sha512_b64url(data: bytes) -> str:
    return "sha512-" + urlsafe_b64encode(hashlib.sha512(data).digest()).rstrip(b"=").decode("ascii")


def _parse_bash(command: str) -> tuple[str | None, Operation]:
    """Best-effort: return (path, operation) for shell commands that touch files."""
    try:
        tokens = shlex.split(command)
    except ValueError:
        return None, "update"
    if not tokens:
        return None, "update"
    head = tokens[0]
    if head == "rm":
        for t in tokens[1:]:
            if not t.startswith("-"):
                return t, "delete"
    if head == "mv" and len(tokens) >= 3:
        return tokens[-1], "update"
    if head == "cp" and len(tokens) >= 3:
        return tokens[-1], "create"
    # Redirections: `>` / `>>` / `tee`
    for i, t in enumerate(tokens):
        if t in (">", ">>") and i + 1 < len(tokens):
            return tokens[i + 1], ("update" if t == ">>" else "create")
        if t == "tee" and i + 1 < len(tokens):
            return tokens[i + 1], "create"
    return None, "update"


def _extract_line_range(text: str | None) -> tuple[int, int] | None:
    if not text:
        return None
    m = _LINE_RANGE_RE.search(text)
    if not m:
        return None
    return (int(m.group(1)), int(m.group(2)))


def _changes_for_claude_code(
    entry: Entry,
    *,
    commit: str | None,
    tool_result_text: str | None,
) -> list[FileChange]:
    """Map Claude Code's editing tools to FileChange records."""
    if not entry.tool_input:
        return []
    name = entry.tool_name or ""
    out: list[FileChange] = []
    if name == "Write":
        path = entry.tool_input.get("file_path")
        content = entry.tool_input.get("content", "")
        if path:
            out.append(
                FileChange(
                    entry_id=entry.entry_id,
                    agent_id=entry.agent_id,
                    path=path,
                    operation="update",
                    line_range=None,
                    content_hash=_sha512_b64url(content.encode("utf-8")),
                    commit=commit,
                    contributor=entry.agent_id,
                )
            )
    elif name == "Edit":
        path = entry.tool_input.get("file_path")
        if path:
            out.append(
                FileChange(
                    entry_id=entry.entry_id,
                    agent_id=entry.agent_id,
                    path=path,
                    operation="update",
                    line_range=_extract_line_range(tool_result_text),
                    content_hash=None,  # post-hoc may be recoverable; left to caller
                    commit=commit,
                    contributor=entry.agent_id,
                )
            )
    elif name == "MultiEdit":
        path = entry.tool_input.get("file_path")
        edits = entry.tool_input.get("edits") or []
        for _ in edits:
            if path:
                out.append(
                    FileChange(
                        entry_id=entry.entry_id,
                        agent_id=entry.agent_id,
                        path=path,
                        operation="update",
                        line_range=None,
                        content_hash=None,
                        commit=commit,
                        contributor=entry.agent_id,
                    )
                )
    elif name == "NotebookEdit":
        path = entry.tool_input.get("notebook_path") or entry.tool_input.get("file_path")
        cell_id = entry.tool_input.get("cell_id") or entry.tool_input.get("cell_index")
        new_source = entry.tool_input.get("new_source", "")
        if path:
            line_range = (int(cell_id), int(cell_id)) if isinstance(cell_id, int) else None
            out.append(
                FileChange(
                    entry_id=entry.entry_id,
                    agent_id=entry.agent_id,
                    path=path,
                    operation="update",
                    line_range=line_range,
                    content_hash=_sha512_b64url(new_source.encode("utf-8")) if new_source else None,
                    commit=commit,
                    contributor=entry.agent_id,
                )
            )
    elif name == "Bash":
        command = entry.tool_input.get("command") or ""
        path, op = _parse_bash(command)
        if path:
            out.append(
                FileChange(
                    entry_id=entry.entry_id,
                    agent_id=entry.agent_id,
                    path=path,
                    operation=op,
                    line_range=None,
                    content_hash=None,
                    commit=commit,
                    contributor=entry.agent_id,
                )
            )
    return out


def derive_file_changes(
    entries: Iterable[Entry],
    *,
    commit: str | None = None,
    tool_name_allowlist: tuple[str, ...] = ("Write", "Edit", "MultiEdit", "NotebookEdit", "Bash"),
    tool_results_by_use_id: dict[str, str] | None = None,
) -> list[FileChange]:
    """Walk tool_call entries and produce FileChange records.

    `tool_results_by_use_id` is an optional map of tool_use_id → tool_result
    text body, used for best-effort line_range extraction on Edit calls.
    """
    results: list[FileChange] = []
    for e in entries:
        if e.kind != "tool_call":
            continue
        if (e.tool_name or "") not in tool_name_allowlist:
            continue
        result_text = None
        if tool_results_by_use_id and e.tool_use_id:
            result_text = tool_results_by_use_id.get(e.tool_use_id)
        results.extend(_changes_for_claude_code(e, commit=commit, tool_result_text=result_text))
    return results
