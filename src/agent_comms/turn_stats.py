"""Statistics settlement derives from the channel's actual pending responses."""

from __future__ import annotations

import asyncio
import secrets
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from . import agent_events as events
from . import pi_commands as commands
from . import turn_phase as phases

if TYPE_CHECKING:
    from .backend import TurnSession
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
            future.done() and not future.cancelled() and future.result().success is True
            for future in self._pending
        )

    @property
    def failed(self) -> bool:
        return any(
            future.cancelled() or future.done() and future.result().success is not True
            for future in self._pending
        )

    @property
    def busy(self) -> bool:
        return any(
            future.done()
            and not future.cancelled()
            and future.result().data is not None
            and future.result().data.session_busy
            for future in self._pending
        )

    async def request(self, session: TurnSession) -> None:
        if session.proc.stdin is None or self.requested:
            return
        self.generation = session.inputs.generation
        self.settlement_count = session.settlement_count
        requests = (
            commands.GetState(
                id=(
                    f"agent-comms-stats-state-{secrets.token_hex(16)}"
                    if session.persistent_session
                    else None
                )
            ),
            commands.GetSessionStats(
                id=(
                    f"agent-comms-stats-usage-{secrets.token_hex(16)}"
                    if session.persistent_session
                    else None
                )
            ),
        )
        self._pending = tuple(session.reader.track(command) for command in requests)
        try:
            for command in requests:
                session.proc.stdin.write(session.reader.command_bytes(command))
            await session.proc.stdin.drain()
        except (BrokenPipeError, ConnectionResetError):
            pass

    async def settle(self, session: TurnSession) -> AsyncIterator[events.AgentEvent]:
        if session.persistent_session is None or not (self.complete or self.failed):
            return
        await asyncio.sleep(0)
        queued = session.steering_queue is not None and not session.steering_queue.empty()
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
        session.finished = True
