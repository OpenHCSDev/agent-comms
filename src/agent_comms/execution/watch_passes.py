from __future__ import annotations

from abc import (
    ABC,
    abstractmethod,
)
from collections.abc import Callable
from contextlib import AbstractContextManager
from functools import partial

from agent_comms.comms import Comms
from agent_comms.execution.child_process import AsyncWork
from agent_comms.nominal.declared_family import DeclaredFamily
from agent_comms.storage.watch_phase import WatchPhase


class Watch(AsyncWork, WatchPhase, DeclaredFamily, ABC):
    """Observe, then apply the result while the bus is open.

    A hold is waited out on the service's own loop, where its stop cancels the wait. Waiting
    in the pass's worker thread kept the connector alive after SIGTERM: the install holding
    maintenance waits for that stop, so systemd killed it at 90 s (installs 17 and 18).
    """

    @abstractmethod
    async def run(self, opened_wire: Callable[[], AbstractContextManager[Comms]]) -> None: ...

    def failed(self, opened_wire: Callable[[], AbstractContextManager[Comms]],
               error: Exception) -> None:
        """A service may report its failed pass; ordinary watches need no extra action."""


class JoinedWatch(Watch):
    """The whole pass is one joined operation under maintenance."""

    async def run(self, opened_wire: Callable[[], AbstractContextManager[Comms]]) -> None:
        with opened_wire() as comms:
            await comms.bus.when_open(partial(
                self.enter_phase, comms.root, partial(self.watch_pass, opened_wire)))

    @abstractmethod
    def watch_pass(self, opened_wire: Callable[[], AbstractContextManager[Comms]]) -> None: ...


class PreparedWatchPass[Prepared, Observed](Watch):
    """Joined effects finish; a source-proved safe read can drop observation."""

    async def run(self, opened_wire: Callable[[], AbstractContextManager[Comms]]) -> None:
        with opened_wire() as comms:
            prepared = await comms.bus.when_open(partial(
                self.enter_phase, comms.root, partial(self.prepare, comms)))
            observed = await self.run_async(partial(
                self.enter_phase, comms.root, partial(self.observe, comms, prepared)))
            await comms.bus.when_open(partial(
                self.enter_phase, comms.root, partial(self.pass_body, comms, observed)))

    @abstractmethod
    def prepare(self, comms: Comms) -> Prepared: ...

    @abstractmethod
    def observe(self, comms: Comms, prepared: Prepared) -> Observed: ...

    @abstractmethod
    def pass_body(self, comms: Comms, observed: Observed) -> None: ...


class WatchPass[Observed](PreparedWatchPass[None, Observed]):
    """A phased watch with no acquisition; only watches with real inputs override preparation."""

    def prepare(self, comms: Comms) -> None:
        pass
