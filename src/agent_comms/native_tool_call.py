"""Track native tool events and the owner's permission separately for each call.

Socket requests can precede streamed events. Only a matching native start
permits the owner's action, and only its callback may consume a stored slot.
These process-local transactions never reconstruct authority from saved evidences.
"""

from __future__ import annotations

import asyncio
import hashlib
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, ClassVar, Never



if TYPE_CHECKING:
    from .native_tool_transport import ToolTransport
    from .envelope_claim_transitions import FileClaimPath
    from .storage_room import RoomReservation


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

    def refuse(self, call: NativeToolCall, refusal: CallClaim) -> None:
        raise SelectedToolDenied("Native tool request/event differs from the declared call")

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
    def refuse(self, call: NativeToolCall, refusal: CallClaim) -> None:
        call.progress.claim_scope = refusal

    def announce(self, call: NativeToolCall) -> None:
        call.progress.observation = AnnouncedCall()


class AnnouncedCall(CallObservation):
    def start(self, call: NativeToolCall) -> None:
        call.progress.observation = ExecutingCall()
        call.progress.started.set()


class ExecutingCall(CallObservation):
    executing = True

    def require_executing(self) -> None:
        pass

    def finish(
        self, call: NativeToolCall, is_error: bool, directory: Path,
        input_id: str, round_number: int,
    ) -> None:
        # A failed terminal fsync cannot be retried or converted to a completed
        # error by a subsequent event. The live acknowledgement must succeed exactly once.
        call.progress.observation = UncertainCall()
        call.progress.claim_scope.finish(call, is_error, directory, input_id, round_number)
        call.progress.observation = CompletedCall()


class CompletedCall(CallObservation):
    complete = True

    def assert_complete(self) -> None:
        pass


class UncertainCall(CallObservation):
    """Interrupted observation; never replay or manufacture a terminal."""


class CallClaim(ABC):  # noqa: B024 - shared rejection; leaves override valid transitions
    def refuse(self, call: NativeToolCall, refusal: CallClaim) -> None:
        raise SelectedToolDenied("Native tool claim was already consumed; cannot replace it")

    def expire(self, call: NativeToolCall) -> None:
        """An unstarted waiter cannot change a tool slot already consumed."""

    async def consume(self, call: NativeToolCall, action: Callable[[], Awaitable[None]]) -> None:
        raise SelectedToolDenied("Native tool request was already consumed; outcome may be UNKNOWN")

    def finish(
        self, call: NativeToolCall, is_error: bool, directory: Path,
        input_id: str, round_number: int,
    ) -> None:
        raise SelectedToolDenied("Native tool ran without owner claim_scope")


class AwaitingClaim(CallClaim):
    def refuse(self, call: NativeToolCall, refusal: CallClaim) -> None:
        call.progress.observation.refuse(call, refusal)

    def expire(self, call: NativeToolCall) -> None:
        call.progress.claim_scope = UncertainClaim()

    async def consume(self, call: NativeToolCall, action: Callable[[], Awaitable[None]]) -> None:
        # No retry even when the callback is cancelled or its durable fsync fails.
        call.progress.claim_scope = UncertainClaim()
        await action()
        call.progress.observation.require_executing()
        call.progress.claim_scope = AuthorizedCall()


class AuthorizedCall(CallClaim):
    def finish(
        self, call: NativeToolCall, is_error: bool, directory: Path,
        input_id: str, round_number: int,
    ) -> None:
        call.commit_terminal(is_error, directory, input_id, round_number)


class UncertainClaim(CallClaim):
    def refuse(self, call: NativeToolCall, refusal: CallClaim) -> None:
        # A later rejection cannot reopen a consumed or uncertain claim.
        pass

    def finish(
        self, call: NativeToolCall, is_error: bool, directory: Path,
        input_id: str, round_number: int,
    ) -> None:
        if not is_error:
            raise SelectedToolDenied("Native tool succeeded without owner claim_scope")
        call.claim_failed()


@dataclass
class CallProgress:
    """The changing execution state, separate from a call's frozen identity and payload."""

    observation: CallObservation = field(default_factory=AwaitingAnnouncement)
    claim_scope: CallClaim = field(default_factory=AwaitingClaim)
    started: asyncio.Event = field(default_factory=asyncio.Event, repr=False)
    room: RoomReservation = field(init=False, repr=False)

    def __post_init__(self):
        # The reservation is mutable call state, not part of its frozen payload.
        from .storage_room import NoRoomNeeded
        self.room = NoRoomNeeded()

    async def finish_room(self):
        from .storage_room import NoRoomNeeded
        await asyncio.to_thread(self.room.finish)
        self.room = NoRoomNeeded()


@dataclass(frozen=True)
class NativeToolCall(ABC):
    """A fixed call payload with one changing execution record."""

    call_id: str
    progress: CallProgress = field(default_factory=CallProgress, init=False, compare=False, repr=False)
    start_wait_seconds: ClassVar[int] = 10
    declared_name: ClassVar[str]

    def correlate(self, candidate: NativeToolCall) -> None:
        if self != candidate:
            candidate.reject_correlated(self)

    def reject_correlated(self, existing: NativeToolCall) -> None:
        raise SelectedToolDenied("Native tool request/event differs from the declared call")

    def announce(self) -> None:
        self.progress.observation.announce(self)

    def start(self) -> None:
        self.progress.observation.start(self)

    async def _wait_started(self) -> None:
        try:
            async with asyncio.timeout(self.start_wait_seconds):
                await self.progress.started.wait()
        except TimeoutError as error:
            raise ToolStartNotSeen() from error

    async def authorize(self, action: Callable[[], Awaitable[None]]) -> None:
        try:
            await self._wait_started()
            self.progress.observation.require_executing()
            await self.progress.claim_scope.consume(self, action)
        except ToolStartNotSeen:
            # The callback never ran. Withdraw permission, but retain the actual
            # native observations: delayed announcement/start/error events
            # must still reconcile this denied call in its declared round.
            self.progress.claim_scope.expire(self)
            self.progress.started.set()
            raise
        except (asyncio.CancelledError, TimeoutError):
            self.progress.observation = UncertainCall()
            self.progress.claim_scope = UncertainClaim()
            self.progress.started.set()  # Wake other waiters; they must fail state validation.
            raise

    def finish(
        self, name: str, is_error: bool | None, directory: Path,
        input_id: str, round_number: int,
    ) -> None:
        if name != self.declared_name or type(is_error) is not bool:
            raise SelectedToolDenied("Native tool terminal identity/result changed")
        self.progress.observation.finish(self, is_error, directory, input_id, round_number)

    @property
    def complete(self) -> bool:
        return self.progress.observation.complete

    def accepts_current_request(self, candidate: NativeToolCall) -> bool:
        if self.complete:
            return False
        self.correlate(candidate)
        return True

    def assert_complete(self) -> None:
        self.progress.observation.assert_complete()

    @abstractmethod
    def commit_terminal(
        self, is_error: bool, directory: Path, input_id: str, round_number: int
    ) -> None: ...

    @abstractmethod
    def claim_failed(self) -> None: ...


@dataclass(frozen=True)
class CodingCall(NativeToolCall):
    @abstractmethod
    def allowed_claim(self) -> FileClaimPath | None: ...

    @abstractmethod
    def digest(self) -> str: ...

    @classmethod
    def storage_effects(cls, cwd):
        """A call without executable effects needs no room."""
        return ()

    def commit_terminal(
        self, is_error: bool, directory: Path, input_id: str, round_number: int
    ) -> None:
        from .selected_tool_broker import record_selected_terminal

        slot = self.slot(input_id, round_number)
        record_selected_terminal(directory, slot, slot)

    def claim_failed(self) -> None:
        # A denied ordinary tool may report an error and let Pi explain it. The
        # consumed call cannot be retried; this grants no mutation authority.
        pass

    @classmethod
    def decode(cls, call_id: object, name: object, arguments: object) -> CodingCall:
        if (
            type(call_id) is not str
            or not call_id
            or len(call_id) > 256
            or type(name) is not str
        ):
            raise SelectedToolDenied("Malformed native coding call identity")
        from .field_codec import MalformedRecord
        from .native_tools import NativeToolDeclaration

        try:
            return NativeToolDeclaration.from_call(name, arguments, call_id=call_id)
        except MalformedRecord as error:
            return RefusedCodingCall(call_id, name, arguments, str(error))

    def slot(self, input_id: str, round_number: int) -> str:
        identity = f"{input_id}\0{round_number}\0{self.call_id}"
        return hashlib.sha256(identity.encode()).hexdigest()[:32]


@dataclass
class ArgumentRefusal(UncertainClaim):
    refusal: RefusedCodingCall

    async def consume(
        self, call: NativeToolCall, action: Callable[[], Awaitable[None]],
    ) -> None:
        raise SelectedToolDenied(self.refusal.reason)


@dataclass(frozen=True)
class RefusedCodingCall(CodingCall):
    """Invalid arguments cannot execute, but still own real start/error events."""

    declared_name: str
    arguments: object
    reason: str

    def __post_init__(self) -> None:
        self.progress.claim_scope = ArgumentRefusal(self)

    def allowed_claim(self) -> Never:
        raise SelectedToolDenied(self.reason)

    def digest(self) -> str:
        return f'REFUSED {self.declared_name}: {" ".join(self.reason.split())[:200]}'

    def correlate(self, candidate: NativeToolCall) -> None:
        self.reject_correlated(candidate)

    def reject_correlated(self, existing: NativeToolCall) -> None:
        # Pi may normalize arguments before its socket arrives. Whichever
        # arrives first keeps the same lifecycle object, never permission.
        if (self.call_id, self.declared_name) != (existing.call_id, existing.declared_name):
            raise SelectedToolDenied("Refused coding call identity changed")
        existing.progress.claim_scope.refuse(existing, self.progress.claim_scope)



@dataclass(frozen=True)
class OwnerToolConnection:
    """The private tool endpoint for one input, never a process setting."""

    path: str
    token: str = field(repr=False)


class NativeToolMode(ABC):
    """Owner-bound native tool policy shared by selected and normal coding turns."""

    @abstractmethod
    def launch_arguments(self, package: Path) -> tuple[str, ...]:
        """Return the native tool selection and reviewed extension."""

    @abstractmethod
    async def open_transport(self, turn, ownership) -> ToolTransport:
        """Open the transport implementing this declared permission."""

    def or_else(self, factory: Callable[[], NativeToolMode]) -> NativeToolMode:
        return self
