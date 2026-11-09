"""Reads used by native wait/settlement callbacks, without importing the backend."""
from __future__ import annotations
from abc import ABC, abstractmethod

from agent_comms import agent_events as events
from agent_comms.execution.native_session_callbacks import NativeSessionCallbacks


class StatsWatchdogRead[Wait](ABC):
    __slots__ = ()
    poll_wait: Wait


class StatsProgressRead[Phase](ABC):
    __slots__ = ()
    phase: Phase


class WatchdogSessionRead[Watchdog, Stats](NativeSessionCallbacks):
    __slots__ = ()
    watchdog: Watchdog
    stats: Stats
    started_input: bool
    started_during_abort: list[events.InputStarted]

    @abstractmethod
    def finish_request(self) -> None: ...

    @abstractmethod
    def ensure_input_forwarding(self) -> None: ...

    @abstractmethod
    async def abort_stalled_rpc(self) -> None: ...
