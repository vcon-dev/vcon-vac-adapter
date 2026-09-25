"""Directory watcher: Claude Code session JSONL files -> vCon -> delivery.

Watches a single, explicitly CONFIGURED directory of Claude Code session
`.jsonl` files (never defaults to the real `~/.claude/projects` — see
`config.Config.watch_dir()`), and for each new-or-changed file, parses it,
builds a vCon, and delivers it via a caller-supplied `deliver` coroutine
(`WebhookDelivery.deliver` or `ConserverDelivery.deliver`).

Idempotency: delivery is tracked per file by content hash (sha256 of the raw
bytes) in a small JSON state file inside `watch_dir` (or wherever
`state_path` points). A file whose hash hasn't changed since the last
successful delivery is skipped; a file that grows or is rewritten (e.g. a
live Claude Code session being appended to) gets a fresh hash and is
reconverted and redelivered as a new vCon for the same `session_id`. State is
only advanced on a successful `deliver()` call, so a failed delivery (which
already lands in the delivery class's own DLQ) is retried on the next pass
rather than silently dropped.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from watchfiles import Change, awatch

from .session_vcon import Granularity, build_vcon
from .sources import claude_code as src_claude_code
from .vcon_builder import LawfulBasisConfig, finalize_vcon

if TYPE_CHECKING:
    pass

log = logging.getLogger(__name__)

Deliver = Callable[[dict[str, Any]], Awaitable[bool]]

_STATE_FILENAME = ".vac-adapter-watch-state.json"


def _hash_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


@dataclass
class _WatchState:
    """Persists `{resolved-file-path: last-delivered-content-hash}`."""

    path: Path
    _hashes: dict[str, str] = field(default_factory=dict)

    def load(self) -> None:
        if self.path.exists():
            try:
                self._hashes = json.loads(self.path.read_text())
            except (json.JSONDecodeError, OSError):
                log.warning("watch_state_unreadable path=%s", self.path)
                self._hashes = {}

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self._hashes, indent=2, sort_keys=True))

    def seen(self, key: str) -> str | None:
        return self._hashes.get(key)

    def mark(self, key: str, digest: str) -> None:
        self._hashes[key] = digest
        self.save()


class SessionWatcher:
    """Watches `watch_dir` for Claude Code `.jsonl` session files and
    delivers a vCon for each file whose content hash hasn't been delivered
    yet.
    """

    def __init__(
        self,
        *,
        watch_dir: str | Path,
        deliver: Deliver,
        state_path: str | Path | None = None,
        granularity: Granularity = "session",
        include_lawful_basis: bool = True,
        lawful_basis_cfg: LawfulBasisConfig | None = None,
    ) -> None:
        self.watch_dir = Path(watch_dir)
        if not self.watch_dir.is_dir():
            raise ValueError(f"watch_dir is not a directory: {self.watch_dir}")
        self._deliver = deliver
        self.granularity: Granularity = granularity
        self.include_lawful_basis = include_lawful_basis
        self.lawful_basis_cfg = lawful_basis_cfg
        default_state_path = self.watch_dir / _STATE_FILENAME
        self.state = _WatchState(Path(state_path) if state_path else default_state_path)
        self.state.load()

    async def process_existing(self) -> int:
        """Process every `.jsonl` already in `watch_dir`. Returns the count
        of sessions successfully delivered (skips unchanged files).
        """
        delivered = 0
        for p in sorted(self.watch_dir.glob("*.jsonl")):
            if await self._process_file(p):
                delivered += 1
        return delivered

    async def _process_file(self, path: Path) -> bool:
        try:
            raw = path.read_bytes()
        except OSError:
            return False
        if not raw.strip():
            return False

        digest = _hash_bytes(raw)
        key = str(path.resolve())
        if self.state.seen(key) == digest:
            return False  # already delivered this exact content

        try:
            session = src_claude_code.parse_file(path)
        except ValueError as e:
            log.warning("watch_parse_skipped path=%s error=%s", path, e)
            return False

        v = build_vcon(
            session,
            granularity=self.granularity,
            include_lawful_basis=self.include_lawful_basis,
            lawful_basis_cfg=self.lawful_basis_cfg,
        )
        vcon_dict = finalize_vcon(v.vcon_dict)

        ok = await self._deliver(vcon_dict)
        if ok:
            self.state.mark(key, digest)
            log.info(
                "watch_delivered",
                extra={"path": str(path), "session_id": session.session_id},
            )
        else:
            log.warning("watch_delivery_failed", extra={"path": str(path)})
        return ok

    async def run(self, *, stop_event: asyncio.Event | None = None) -> None:
        """Deliver every existing file once, then watch `watch_dir` for new
        or changed `.jsonl` files until `stop_event` is set (or forever, if
        no `stop_event` is given).
        """
        await self.process_existing()
        async for changes in awatch(self.watch_dir, stop_event=stop_event):
            for change, changed_path in changes:
                if change == Change.deleted:
                    continue
                p = Path(changed_path)
                if p.suffix != ".jsonl":
                    continue
                await self._process_file(p)
