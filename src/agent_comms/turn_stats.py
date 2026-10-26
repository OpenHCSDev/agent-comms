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
    from .backend import FinishSignal, NativeSession
    from .turn_watchdog import ProgressWatchdog, TurnWait


class StatsRequest:
    """No native settlement observation is waiting for its snapshot."""

    def wait_for(self, watchdog: ProgressWatchdog, active: TurnWait) -> TurnWait:
        return active

    def finish_waiters(self, signal: FinishSignal) -> tuple[asyncio.Task[bool], ...]:
        return signal.waiters()

    def permits_live_status(self, session: NativeSession) -> bool:
        return session.prompt_state.permits_extension_ui(session)

    @property
    def complete(self) -> bool:
        return False

    @property
    def tasks(self) -> tuple[asyncio.Task[ReadingStats], ...]:
        return ()

    async def request(self, session: NativeSession) -> None:
        # A newer native settlement replaces an older in-flight snapshot. Its
        # receipt, not a copied event counter, determines the current operation.
        await self.close()
        session.stats = RequestingStats(asyncio.create_task(self._request(session)))

    @staticmethod
    async def _request(session: NativeSession) -> ReadingStats:
        # Admission has stopped. No get_state query may overtake an input still
        # in async prompt preflight; join its original RPC acknowledgement first.
        await session.native.reader.pending.before_settlement()
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

    def finish_waiters(self, signal: FinishSignal) -> tuple[asyncio.Task[bool], ...]:
        return ()

    def permits_live_status(self, session: NativeSession) -> bool:
        return False


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

    def wait_for(self, watchdog: ProgressWatchdog, active: TurnWait) -> TurnWait:
        # Only the metadata replies use the short snapshot wait. A prompt still
        # in preflight keeps its active input/model/UI wait in RequestingStats.
        return watchdog.poll_wait

    @property
    def complete(self) -> bool:
        return all(entry.future.done() and not entry.future.cancelled()
                   and entry.future.result().success.proves_idle(entry.future.result())
                   for entry in self.pending)

    async def close(self) -> None:
        for entry in self.pending:
            entry.future.cancel()

    async def settle(self, session: NativeSession) -> AsyncIterator[events.AgentEvent]:
        if not all(entry.future.done() for entry in self.pending):
            return
        snapshots = tuple(entry.future.result().success.require_data(entry.future.result())
                          for entry in self.pending)
        if any(snapshot.session_busy for snapshot in snapshots):
            # Already-written inputs can start another native run. The next
            # agent_settled observation owns its snapshot; do not poll the model.
            session.stats = StatsRequest()
            session.watchdog.phase = phases.ModelWaitPhase()
            return
        # A proved idle child cannot start an input it still owes Core.
        # Report that UNKNOWN immediately, rather than manufacture model wait.
        session.prompt_state.validate_terminal(session, transport_ok=True)
        yield events.StreamSettled()
        session.finish_request()
