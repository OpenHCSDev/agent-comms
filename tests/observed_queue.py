"""What the real observer receiver delivers, in receive order, for tests that watch it.

``ObservationProcess.start_receiving`` owns reading the pipe; a test passes this queue's
callbacks to it once and waits here. A receiver failure is raised as its original exception
when the waiting test reaches it, never swallowed or replaced.
"""
from __future__ import annotations

import asyncio
from collections.abc import Callable

from agent_comms.ui_model.observation import Observed


class ObservedQueue:
    def __init__(self) -> None:
        self._delivered: asyncio.Queue[Callable[[], Observed]] = asyncio.Queue()

    def observed(self, result: Observed) -> None:
        self._delivered.put_nowait(lambda: result)

    def failed(self, error: Exception) -> None:
        def raise_original() -> Observed:
            raise error

        self._delivered.put_nowait(raise_original)

    async def next(self) -> Observed:
        async with asyncio.timeout(30):
            delivered = await self._delivered.get()
        return delivered()

    async def until(self, accepted: Callable[[Observed], bool]) -> Observed:
        while not accepted(result := await self.next()):
            pass
        return result
