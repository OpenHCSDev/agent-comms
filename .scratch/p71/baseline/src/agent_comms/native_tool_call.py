"""Per-call native observation and owner admission; neither fact implies the other.

Socket requests can precede streamed events. Only a correlated native start
unblocks admission, and only the owner's callback may consume a durable slot.
These process-local transactions never reconstruct authority from saved receipts.
"""

from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import ClassVar


class SelectedToolDenied(ValueError):  # noqa: N818 - nominal fail-closed outcome
    """A rejected request, including duplicate or uncertain prior consumption."""


class DuplicateToolCallIdentity(SelectedToolDenied):
    """One assistant tool round declared the same provider call identity twice."""

    def __init__(self, call_id: str) -> None:
        super().__init__(f"Native tool round repeats call identity {call_id!r}")


class CoordinationBusy(SelectedToolDenied):
    """The coordination database stayed busy; the call was not checked and nothing changed."""

    def __init__(self) -> None:
        super().__init__(
            "The coordination database is busy. This tool call did not run and "
            "changed nothing; call the tool again."
        )


class ToolStartNotSeen(SelectedToolDenied):
    """Pi asked to run a tool call it never reported as started; the call was not checked."""

    def __init__(self) -> None:
        super().__init__(
            "Pi did not report this tool call as started within "
            f"{NativeToolCall.start_wait_seconds} s, so it was not checked. "
            "The tool was not started; call it again."
        )


class CallObservation(ABC):  # noqa: B024 - shared rejection; leaves override valid transitions
    executing = False
    complete = False

    def announce(self, call: NativeToolCall) -> None:
        raise SelectedToolDenied("Native tool call announcement was repeated or invalid")

    def start(self, call: NativeToolCall) -> None:
        raise SelectedToolDenied("Native tool start is missing an announcement or was repeated")

    def require_executing(self) -> None:
        raise SelectedToolDenied("Native tool is not executing; outcome may be UNKNOWN")

    def finish(
        self, call: NativeToolCall, is_error: bool, directory: Path,
        input_id: str, round_number: int,
    ) -> None:
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
    executing = True

    def require_executing(self) -> None:
        pass

    def finish(
        self, call: NativeToolCall, is_error: bool, directory: Path,
        input_id: str, round_number: int,
    ) -> None:
        # A failed terminal fsync cannot be retried or converted to a completed
        # error by a subsequent event. The live receipt must succeed exactly once.
        call.observation = UncertainCall()
        call.admission.finish(call, is_error, directory, input_id, round_number)
        call.observation = CompletedCall()


class CompletedCall(CallObservation):
    complete = True

    def assert_complete(self) -> None:
        pass


class UncertainCall(CallObservation):
    """Interrupted observation; never replay or manufacture a terminal."""


class CallAdmission(ABC):  # noqa: B024 - shared rejection; leaves override valid transitions
    def expire(self, call: NativeToolCall) -> None:
        """An unstarted waiter cannot change an already consumed admission."""

    async def consume(self, call: NativeToolCall, action: Callable[[], Awaitable[None]]) -> None:
        raise SelectedToolDenied("Native tool request was already consumed; outcome may be UNKNOWN")

    def finish(
        self, call: NativeToolCall, is_error: bool, directory: Path,
        input_id: str, round_number: int,
    ) -> None:
        raise SelectedToolDenied("Native tool ran without owner admission")


class AwaitingAdmission(CallAdmission):
    def expire(self, call: NativeToolCall) -> None:
        call.admission = UncertainAdmission()

    async def consume(self, call: NativeToolCall, action: Callable[[], Awaitable[None]]) -> None:
        # No retry even when the callback is cancelled or its durable fsync fails.
        call.admission = UncertainAdmission()
        await action()
        call.observation.require_executing()
        call.admission = AdmittedCall()


class AdmittedCall(CallAdmission):
    def finish(
        self, call: NativeToolCall, is_error: bool, directory: Path,
        input_id: str, round_number: int,
    ) -> None:
        call.commit_terminal(is_error, directory, input_id, round_number)


class UncertainAdmission(CallAdmission):
    def finish(
        self, call: NativeToolCall, is_error: bool, directory: Path,
        input_id: str, round_number: int,
    ) -> None:
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
    start_wait_seconds: ClassVar[int] = 10

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

    async def _wait_started(self) -> None:
        try:
            async with asyncio.timeout(self.start_wait_seconds):
                await self.started.wait()
        except TimeoutError as error:
            raise ToolStartNotSeen() from error

    async def authorize(self, action: Callable[[], Awaitable[None]]) -> None:
        try:
            await self._wait_started()
            self.observation.require_executing()
            await self.admission.consume(self, action)
        except ToolStartNotSeen:
            # The callback never ran. Close admission, but retain the actual
            # native observations: delayed announcement/start/error events
            # must still reconcile this denied call in its declared round.
            self.admission.expire(self)
            self.started.set()
            raise
        except (asyncio.CancelledError, TimeoutError):
            self.observation = UncertainCall()
            self.admission = UncertainAdmission()
            self.started.set()  # Wake other waiters; they must fail state validation.
            raise

    def finish(
        self, name: str, is_error: bool | None, directory: Path,
        input_id: str, round_number: int,
    ) -> None:
        if name != self.name or type(is_error) is not bool:
            raise SelectedToolDenied("Native tool terminal identity/result changed")
        self.observation.finish(self, is_error, directory, input_id, round_number)

    @property
    def complete(self) -> bool:
        return self.observation.complete

    def accepts_current_request(self, candidate: NativeToolCall) -> bool:
        if self.complete:
            return False
        self.correlate(candidate)
        return True

    def assert_complete(self) -> None:
        self.observation.assert_complete()

    @abstractmethod
    def commit_terminal(
        self, is_error: bool, directory: Path, input_id: str, round_number: int
    ) -> None: ...

    @abstractmethod
    def admission_failed(self) -> None: ...
