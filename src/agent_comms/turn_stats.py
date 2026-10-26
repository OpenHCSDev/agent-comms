"""Native settlement closes admission, joins actual receipts and proves the child idle."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Collection
from dataclasses import dataclass
from typing import TYPE_CHECKING

from . import agent_events as events
from . import pi_commands as commands
from . import turn_failure as failures
from . import turn_phase as phases
from .pending_requests import PendingRequest

if TYPE_CHECKING:
    from .backend import NativeSession


class StatsRequest:
    """No native settlement observation is waiting for its snapshot."""

    requested = False
    complete = False
    tasks: tuple[asyncio.Task[ReadingStats], ...] = ()

    async def request(self, session: NativeSession) -> None:
        # A newer native settlement replaces an older in-flight snapshot. Its
        # receipt, not a copied event counter, determines the current operation.
        await self.close()
        session.stats = RequestingStats(asyncio.create_task(self._request(session)))

    @staticmethod
    async def _request(session: NativeSession) -> ReadingStats:
        # Admission has stopped. No get_state query may overtake an input still
        # in async prompt preflight; join its original RPC acknowledgement first.
        await session.inputs.responses()
        pending = (
            session.native.send(commands.GetState()),
            session.native.send(commands.GetSessionStats()),
        )
        await session.native.drain()
        return ReadingStats(pending)

    def advance_if_done(self, session: NativeSession, completed: Collection[object]) -> bool:
        return False

    async def close(self) -> None:
        pass

    def expired(self, session: NativeSession) -> None:
        session.output.record_failure(failures.BackendFailed(
            "Pi did not finish its native settlement snapshot."
        ))

    async def settle(self, session: NativeSession) -> AsyncIterator[events.AgentEvent]:
        return
        yield


class RequestedStats(StatsRequest):
    """A native settlement snapshot is in flight or retained as its evidence."""

    requested = True


@dataclass(frozen=True)
class RequestingStats(RequestedStats):
    task: asyncio.Task[ReadingStats]

    @property
    def tasks(self) -> tuple[asyncio.Task[ReadingStats], ...]:
        return (self.task,)

    def advance_if_done(self, session: NativeSession, completed: Collection[object]) -> bool:
        if self.task not in completed:
            return False
        session.stats = self.task.result()
        return True

    async def close(self) -> None:
        self.task.cancel()
        await asyncio.gather(self.task, return_exceptions=True)
        if not self.task.cancelled() and self.task.exception() is None:
            await self.task.result().close()


@dataclass(frozen=True)
class ReadingStats(RequestedStats):
    pending: tuple[PendingRequest[commands.GetState], PendingRequest[commands.GetSessionStats]]

    @property
    def complete(self) -> bool:
        return all(entry.future.done() and not entry.future.cancelled()
                   and entry.future.result().succeeded
                   and not entry.future.result().data.session_busy
                   for entry in self.pending)

    async def close(self) -> None:
        for entry in self.pending:
            entry.future.cancel()

    async def settle(self, session: NativeSession) -> AsyncIterator[events.AgentEvent]:
        if not all(entry.future.done() for entry in self.pending):
            return
        if any(entry.future.result().data.session_busy for entry in self.pending):
            # Already-written inputs can start another native run. The next
            # agent_settled observation owns its snapshot; do not poll the model.
            session.stats = StatsRequest()
            session.watchdog.phase = phases.ModelWaitPhase()
            return
        # A proved idle child cannot start an input it still owes Core.
        # Report that UNKNOWN immediately, rather than manufacture model wait.
        session.output.settle(session, transport_ok=True)
        yield events.StreamSettled()
        session.finish_request()
