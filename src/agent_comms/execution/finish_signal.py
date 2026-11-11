from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod
from dataclasses import dataclass

from agent_comms.nominal.field_codec import boundary_decoder


class FinishSignal(ABC):
    """Own the optional external finish wait, without a nullable task in the reader."""

    @classmethod
    @boundary_decoder
    def from_event(cls, event: asyncio.Event | None) -> FinishSignal:
        return NoFinishSignal() if event is None else EventFinishSignal(event)

    @abstractmethod
    def waiters(self) -> tuple[asyncio.Task[bool], ...]: ...

    def consumed(self) -> None:
        pass


class NoFinishSignal(FinishSignal):
    def waiters(self) -> tuple[asyncio.Task[bool], ...]:
        return ()


@dataclass(frozen=True)
class EventFinishSignal(FinishSignal):
    event: asyncio.Event

    def waiters(self) -> tuple[asyncio.Task[bool], ...]:
        return (asyncio.create_task(self.event.wait()),)

    def consumed(self) -> None:
        self.event.clear()


NO_FINISH_SIGNAL = NoFinishSignal()
