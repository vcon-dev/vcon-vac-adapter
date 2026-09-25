"""End-to-end test of the daemon-mode session watcher against a temp fixture
directory. NEVER points at a real `~/.claude` directory — `watch_dir` is
always a pytest `tmp_path`.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from vcon_vac_adapter.watcher import SessionWatcher

FIXTURE = (
    Path(__file__).parent / "fixtures" / "claude_code" / "simple_session.jsonl"
).read_text()

_EXTRA_TURN = (
    '{"type":"user","uuid":"u3","parentUuid":"a2","timestamp":"2026-05-22T18:00:04.000Z",'
    '"sessionId":"sess-abc","cwd":"/tmp/proj","gitBranch":"main","version":"1.2.0",'
    '"message":{"role":"user","content":"Thanks!"}}\n'
)


class _RecordingDelivery:
    def __init__(self, *, succeed: bool = True) -> None:
        self.delivered: list[dict] = []
        self.succeed = succeed

    async def deliver(self, vcon_dict: dict) -> bool:
        if self.succeed:
            self.delivered.append(vcon_dict)
        return self.succeed


@pytest.mark.asyncio
async def test_process_existing_delivers_each_session_once(tmp_path) -> None:
    (tmp_path / "sess-abc.jsonl").write_text(FIXTURE)
    delivery = _RecordingDelivery()
    watcher = SessionWatcher(watch_dir=tmp_path, deliver=delivery.deliver)

    count = await watcher.process_existing()

    assert count == 1
    assert len(delivery.delivered) == 1
    assert delivery.delivered[0]["vcon"] == "0.4.0"
    assert "agent_session" in delivery.delivered[0]["extensions"]


@pytest.mark.asyncio
async def test_process_existing_is_idempotent_on_unchanged_content(tmp_path) -> None:
    (tmp_path / "sess-abc.jsonl").write_text(FIXTURE)
    delivery = _RecordingDelivery()
    watcher = SessionWatcher(watch_dir=tmp_path, deliver=delivery.deliver)

    first = await watcher.process_existing()
    second = await watcher.process_existing()

    assert first == 1
    assert second == 0  # unchanged content hash -> skipped
    assert len(delivery.delivered) == 1


@pytest.mark.asyncio
async def test_changed_file_is_redelivered(tmp_path) -> None:
    session_path = tmp_path / "sess-abc.jsonl"
    session_path.write_text(FIXTURE)
    delivery = _RecordingDelivery()
    watcher = SessionWatcher(watch_dir=tmp_path, deliver=delivery.deliver)
    await watcher.process_existing()

    session_path.write_text(FIXTURE + _EXTRA_TURN)
    count = await watcher.process_existing()

    assert count == 1
    assert len(delivery.delivered) == 2


@pytest.mark.asyncio
async def test_failed_delivery_is_retried_next_pass(tmp_path) -> None:
    (tmp_path / "sess-abc.jsonl").write_text(FIXTURE)
    delivery = _RecordingDelivery(succeed=False)
    watcher = SessionWatcher(watch_dir=tmp_path, deliver=delivery.deliver)

    first = await watcher.process_existing()
    delivery.succeed = True
    second = await watcher.process_existing()

    assert first == 0
    assert second == 1  # not marked delivered after the failure, so retried


@pytest.mark.asyncio
async def test_run_watches_for_new_files(tmp_path) -> None:
    delivery = _RecordingDelivery()
    watcher = SessionWatcher(watch_dir=tmp_path, deliver=delivery.deliver)
    stop_event = asyncio.Event()

    async def _writer_then_stop() -> None:
        await asyncio.sleep(0.3)
        (tmp_path / "sess-abc.jsonl").write_text(FIXTURE)
        await asyncio.sleep(1.0)
        stop_event.set()

    await asyncio.gather(watcher.run(stop_event=stop_event), _writer_then_stop())

    assert len(delivery.delivered) == 1


def test_watch_dir_must_exist(tmp_path) -> None:
    missing = tmp_path / "does-not-exist"
    with pytest.raises(ValueError, match="not a directory"):
        SessionWatcher(watch_dir=missing, deliver=_RecordingDelivery().deliver)


def test_watch_dir_never_defaults_to_real_home() -> None:
    """Regression guard: constructing a watcher requires an explicit,
    already-existing `watch_dir`; there is no code path that falls back to
    the real `~/.claude/projects`.
    """
    import inspect

    sig = inspect.signature(SessionWatcher.__init__)
    assert sig.parameters["watch_dir"].default is inspect.Parameter.empty
