"""Source protocol for streaming sessions from a platform."""

from __future__ import annotations

from typing import AsyncIterator, Protocol, runtime_checkable

from ..ir import Session


@runtime_checkable
class Source(Protocol):
    """Yield Sessions as they appear on the source platform.

    Implementations must not block the event loop on parsing — they should be
    safely awaitable from the daemon's `run()` loop.
    """

    async def stream(self) -> AsyncIterator[Session]: ...
