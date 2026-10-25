"""Statistics settlement derives from the channel's actual pending responses."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from agent_comms.pi_payloads import RequestId

from . import agent_events as events
from . import pi_commands as commands
from . import turn_phase as phases
from .turn_item import PiRequestKey

if TYPE_CHECKING:
    from .backend import NativeSession
    from .pi_events import Response


@dataclass
class StatsRequest:
    _pending: tuple[asyncio.Future[Response], ...] = field(default=(), repr=False)
    generation: int = 0
    settlement_count: int = 0

    @property
    def requested(self) -> bool:
        return bool(self._pending)

    @property
    def complete(self) -> bool:
        return self.requested and all(
            future.done() and not future.cancelled() and future.result().succeeded
            for future in self._pending
        )

    @property
    def failed(self) -> bool:
        return any(
            future.cancelled() or future.done() and not future.result().succeeded
            for future in self._pending
        )

    @property
    def busy(self) -> bool:
        return any(
            future.done()
            and not future.cancelled()
            and future.result().data.session_busy
            for future in self._pending
        )

    async def request(self, session: NativeSession) -> None:
        if self.requested:
            return
        self.generation = session.inputs.generation
        self.settlement_count = session.settlement_count
        requests = (
            commands.GetState(id=RequestId(PiRequestKey.fresh())),
            commands.GetSessionStats(
                id=RequestId(PiRequestKey.fresh())
            ),
        )
        self._pending = tuple(session.native.send(command) for command in requests)
        await session.native.drain()

    async def settle(self, session: NativeSession) -> AsyncIterator[events.AgentEvent]:
        if not (self.complete or self.failed):
            return
        await asyncio.sleep(0)
        queued = session.inputs.queue.has_ready_work
        if not self.failed and (
            self.busy
            or session.inputs.pending
            or queued
            or session.inputs.generation != self.generation
        ):
            self._pending = ()
            session.watchdog.phase = phases.ModelWaitPhase()
            if (
                session.settlement_count > self.settlement_count or queued
            ) and not session.inputs.pending:
                await self.request(session)
            session.skip = True
            return
        yield events.StreamSettled()
        session.finish_request()
