"""Per-call native observation and owner admission; neither fact implies the other.

Socket requests can precede streamed events. Only a correlated native start
unblocks admission, and only the owner's callback may consume a durable slot.
These process-local transactions never reconstruct authority from saved receipts.
"""

from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path


class SelectedToolDenied(ValueError):  # noqa: N818 - nominal fail-closed outcome
    """A rejected request, including duplicate or uncertain prior consumption."""


class CallObservation(ABC):  # noqa: B024 - shared rejection; leaves override valid transitions
    def announce(self, call: NativeToolCall) -> None:
        raise SelectedToolDenied("Native tool call announcement was repeated or invalid")

    def start(self, call: NativeToolCall) -> None:
        raise SelectedToolDenied("Native tool start is missing an announcement or was repeated")

    def require_executing(self) -> None:
        raise SelectedToolDenied("Native tool is not executing; outcome may be UNKNOWN")

    def finish(self, call: NativeToolCall, is_error: bool, directory: Path, input_id: str) -> None:
        raise SelectedToolDenied("Native tool terminal is missing or repeated")

    def assert_complete(self) -> None:
        raise SelectedToolDenied("Native tool outcome remains UNKNOWN")


class AwaitingAnnouncement(CallObservation):
    def announce(self, call: NativeToolCall) -> None:
        call.observation = AnnouncedCall()


class AnnouncedCall(CallObservation):
    def start(self, call: NativeToolCall) -> None:
        call.observation = ExecutingCall()
        call.started.set()


class ExecutingCall(CallObservation):
    def require_executing(self) -> None:
        pass

    def finish(self, call: NativeToolCall, is_error: bool, directory: Path, input_id: str) -> None:
        # A failed terminal fsync cannot be retried or converted to a completed
        # error by a subsequent event. The live receipt must succeed exactly once.
        call.observation = UncertainCall()
        call.admission.finish(call, is_error, directory, input_id)
        call.observation = CompletedCall()


class CompletedCall(CallObservation):
    def assert_complete(self) -> None:
        pass


class UncertainCall(CallObservation):
    """Interrupted observation; never replay or manufacture a terminal."""


class CallAdmission(ABC):  # noqa: B024 - shared rejection; leaves override valid transitions
    def consume(self, call: NativeToolCall, action: Callable[[], None]) -> None:
        raise SelectedToolDenied("Native tool request was already consumed; outcome may be UNKNOWN")

    def finish(self, call: NativeToolCall, is_error: bool, directory: Path, input_id: str) -> None:
        raise SelectedToolDenied("Native tool ran without owner admission")


class AwaitingAdmission(CallAdmission):
    def consume(self, call: NativeToolCall, action: Callable[[], None]) -> None:
        # No retry even when the callback is cancelled or its durable fsync fails.
        call.admission = UncertainAdmission()
        action()
        call.admission = AdmittedCall()


class AdmittedCall(CallAdmission):
    def finish(self, call: NativeToolCall, is_error: bool, directory: Path, input_id: str) -> None:
        call.commit_terminal(is_error, directory, input_id)


class UncertainAdmission(CallAdmission):
    def finish(self, call: NativeToolCall, is_error: bool, directory: Path, input_id: str) -> None:
        if not is_error:
            raise SelectedToolDenied("Native tool succeeded without owner admission")
        call.admission_failed()


@dataclass
class NativeToolCall(ABC):
    """One payload owner with orthogonal live observation/admission state."""

    call_id: str
    observation: CallObservation = field(
        default_factory=AwaitingAnnouncement, init=False, compare=False
    )
    admission: CallAdmission = field(default_factory=AwaitingAdmission, init=False, compare=False)
    # Notification only, not a second authority for determining completion.
    started: asyncio.Event = field(
        default_factory=asyncio.Event, init=False, compare=False, repr=False
    )

    @property
    @abstractmethod
    def name(self) -> str: ...

    def correlate(self, candidate: NativeToolCall) -> None:
        if self != candidate:
            raise SelectedToolDenied("Native tool request/event differs from the declared call")

    def announce(self) -> None:
        self.observation.announce(self)

    def start(self) -> None:
        self.observation.start(self)

    async def authorize(self, action: Callable[[], None]) -> None:
        try:
            async with asyncio.timeout(10):
                await self.started.wait()
            self.observation.require_executing()
            self.admission.consume(self, action)
        except (asyncio.CancelledError, TimeoutError):
            self.observation = UncertainCall()
            self.admission = UncertainAdmission()
            self.started.set()  # Wake other waiters; they must fail state validation.
            raise

    def finish(self, name: str, is_error: bool | None, directory: Path, input_id: str) -> None:
        if name != self.name or type(is_error) is not bool:
            raise SelectedToolDenied("Native tool terminal identity/result changed")
        self.observation.finish(self, is_error, directory, input_id)

    def assert_complete(self) -> None:
        self.observation.assert_complete()

    @abstractmethod
    def commit_terminal(self, is_error: bool, directory: Path, input_id: str) -> None: ...

    @abstractmethod
    def admission_failed(self) -> None: ...
