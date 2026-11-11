"""Core observation for the comms UI, in a process of its own.

A UI process asks what to observe (the sidebar, the threads its views show)
and receives finished results only when Core's stores actually change: the
service watches the store files that ``HistoryViews.revision_paths`` declares,
reads snapshots and presentations, derives the sidebar rows, and sends them
over a pipe. The UI also asks it for reads it awaits: store reads (transcript
pages, their currency, message notifications), answered in arrival order, and
thread-scoped reads (the goal snapshot, input delivery), answered as they
complete through the shared operation's live-owner or retained-evidence contract.
Each answer carries the request id and the decoded value or the exception the read raised. Store work runs in the
service process. The receiving process reads and unpickles each complete packet
in a joined worker; only typed delivery and future settlement run on its loop.
The receiving worker still shares its interpreter lock with the UI.
"""

from __future__ import annotations

import asyncio
import itertools
import os
import signal
import socket
import struct
import sys
import time
import traceback
from abc import ABC, abstractmethod
from collections.abc import Callable, Generator, Iterator, Mapping
from concurrent.futures import Future
from contextlib import AbstractAsyncContextManager, AsyncExitStack, ExitStack, aclosing
from dataclasses import dataclass, field, replace
from functools import cached_property, partial
from multiprocessing.connection import Connection
from multiprocessing.process import BaseProcess
from pathlib import Path
from typing import ClassVar, Self, cast, final, get_type_hints

from aiohttp import ClientWebSocketResponse, WSMsgType, web
from metaclass_registry.mro_dispatch import MroDispatch, handles

from agent_comms.acp_extension import (
    GoalSnapshotReply,
    TranscriptDraftUpdate,
    TranscriptSnapshotUpdate,
)
from agent_comms.comms import Comms
from agent_comms.coordination.coordinator import Coordination
from agent_comms.coordination.errors import (
    CoordinationError,
    CoordinationReadUnavailable,
    DeferredRead,
    ReadAgain,
    Refused,
    ReloadRequired,
    StaleRevision,
)
from agent_comms.errors import RelationViolationError
from agent_comms.execution.child_process import HelperStart, SpawnedHelper, join_retirement
from agent_comms.execution.launch_root import started
from agent_comms.history_views import RetiredViews
from agent_comms.input_disposition import DeliveryOverview
from agent_comms.installer.active_route import CommsRoute
from agent_comms.messaging.bus_read import CapturesTranscriptPage, OwnerScopedRead, UiRead
from agent_comms.messaging.channel_targets import CommandTarget
from agent_comms.messaging.message_notification import (
    MessageNotification,
    NotificationPass,
    NotificationWindow,
)
from agent_comms.messaging.message_reference import MessageReference
from agent_comms.messaging.notification_reads import NotificationRead
from agent_comms.messaging.read_basis import DisplayCut, NoDisplaySelection
from agent_comms.messaging.revision_views import (
    BusSourceView,
    NotificationsView,
    RegistrationsView,
    RevisionView,
    SidebarView,
    StatusView,
    StoresView,
    TeamBoardView,
    TeamFramesView,
)
from agent_comms.messaging.watched import FileSource
from agent_comms.transport.tool_caller import HumanCaller
from agent_comms.transport.tool_request import CoreRead, ServedRequest
from agent_comms.nominal.field_codec import (
    DeclaredError,
    FieldCodec,
    MessageId,
    TaggedRecordMember,
    TaggedUnion,
)
from agent_comms.nominal.fold import Absorbs, Accumulates, Fold
from agent_comms.notification_read import (
    NotificationsRead,
    StaleNotificationsRead,
    UnreadableNotificationsRead,
)
from agent_comms.presentation import CoordinationSnapshot, NoRevision, ReadState, WireRevision
from agent_comms.projects.checkouts import Checkout
from agent_comms.projects.work_branches import WorkBranchError
from agent_comms.registry.activity import ObservedActivity
from agent_comms.registry.registry_document import RegistrySnapshot, UnreadRegistry
from agent_comms.registry.registry_names import DeletedThread
from agent_comms.registry.roster_visibility import RosterVisibility
from agent_comms.registry.thread_identity import ThreadIncarnation
from agent_comms.registry.thread_presentation import ThreadPresentation
from agent_comms.registry.view_unread import ReplyIndexResult
from agent_comms.team.team_heartbeat import TeamBoardRead
from agent_comms.team.team_hud import TeamFrame, TeamHud, TeamPlan
from agent_comms.team.team_hud_records import TeammateDetail
from agent_comms.transcript.transcripts import (
    AcquiredThreadPresentation,
    TranscriptRead,
    TranscriptReadIdentity,
)
from agent_comms.transport.request import ToolRequest
from agent_comms.transport.tool_result import ToolResult
from agent_comms.transport.tools.reads import TranscriptPageRead
from agent_comms.ui_model.sidebar import (
    ChannelRowModel,
    SidebarModel,
    ThreadRowModel,
    WaitingSidebar,
)
from agent_comms.ui_model.status import OpenThreadStatus, ThreadStatusModel
from agent_comms_hooks import ToolRefusedError

type WebSocketFrames = ClientWebSocketResponse | web.WebSocketResponse

class ObservationRequest(TaggedUnion, affix=""):
    """A UI request; each declares how the service receives it. A remote UI sends it over
    the connector, so each record carries its kind."""


    @abstractmethod
    def receive(self, service: ObservationService) -> None: ...


class InterestRequest(TaggedRecordMember, ObservationRequest, declared_name=None):
    """Changes what is observed; the service observes again."""

    @abstractmethod
    def observation(self) -> Observation: ...

    def receive(self, service: ObservationService) -> None:
        service.interest.receive(self, service)


class ObservationInterestStopped(RuntimeError):
    """The observer still serves acquired reads but accepts no new interests."""


class ObservationInterest(ABC):
    @abstractmethod
    def receive(self, request: InterestRequest, service: ObservationService) -> None: ...

    @abstractmethod
    def stop(self) -> ObservationInterest: ...

    def remove(self, kind: type[Observation]) -> None:
        pass

    def prepared(self, observation: ReadingObservation, service: ObservationService) -> None:
        pass

    async def observe(self, service: ObservationService, revision: WireRevision,
                      attempted: set[int]) -> None:
        pass


class StoppedObservationInterest(ObservationInterest):
    def receive(self, request: InterestRequest, service: ObservationService) -> None:
        raise ObservationInterestStopped("The observer has stopped accepting interests")

    def stop(self) -> ObservationInterest:
        return self


class LiveObservationInterest(ObservationInterest):
    def __init__(self) -> None:
        self.observations: dict[type[Observation], Observation] = {}

    def receive(self, request: InterestRequest, service: ObservationService) -> None:
        observation = request.observation()
        kind = type(observation)
        self.observations[kind] = self.observations.get(kind, NotObserved()).for_interest(observation)
        service.observe_again()

    def stop(self) -> ObservationInterest:
        for observation in self.observations.values():
            observation.close()
        self.observations.clear()
        return StoppedObservationInterest()

    def remove(self, kind: type[Observation]) -> None:
        self.observations.pop(kind, NotObserved()).close()

    def prepared(self, observation: ReadingObservation, service: ObservationService) -> None:
        if self.observations.get(type(observation)) is observation:
            observation.invalidate()
            service.observe_again()

    async def observe(self, service: ObservationService, revision: WireRevision,
                      attempted: set[int]) -> None:
        observations = tuple(self.observations.items())
        for kind, observation in observations:
            await service.answer_pending(attempted)
            if self.observations.get(kind) is not observation:
                continue
            observation.retire_step(service, revision)
            await service.answer_pending(attempted)
        for kind, observation in observations:
            await service.answer_pending(attempted)
            if self.observations.get(kind) is not observation:
                continue
            observation.step(service, revision)
            await service.answer_pending(attempted)


class ObservationLifetime:
    def for_interest(self, requested: Observation) -> Observation:
        return requested

    def close(self) -> None:
        """Retire any unfinished read owned by this interest."""


class NotObserved(ObservationLifetime):
    pass


class ObservationStep:
    def publish(self, service: ObservationService) -> None:
        pass


class ObservationPending(ObservationStep):
    pass


class Observation[InterestT: InterestRequest](ObservationLifetime):
    """Retire a revision's vanished sources before presenting its observations."""

    def __init__(self, request: InterestT):
        self.request = request

    def for_interest(self, requested: Observation) -> Observation:
        if self.request == requested.request:
            return self
        self.close()
        return requested.continue_from(self)

    def continue_from(self, previous: Self) -> Self:
        """A changed interest starts fresh unless its reader can retain useful answers."""
        return self

    def retire_step(self, service: ObservationService, revision: WireRevision) -> None:
        """Most observations do not retire views."""

    def step(self, service: ObservationService, revision: WireRevision) -> None:
        """Retirement-only observations have no presentation work."""


class ReadingObservation[InterestT: InterestRequest](Observation[InterestT]):
    """A requested reader yields between store operations in the presentation phase."""

    view: ClassVar[RevisionView]

    def __init_subclass__(cls, **kwargs: object) -> None:
        super().__init_subclass__(**kwargs)
        try:
            cls.view
        except AttributeError as error:
            raise TypeError(f"{cls.__name__} must declare its revision view") from error

    def __init__(self, request: InterestT):
        super().__init__(request)
        self.steps: Generator[ObservationStep] = (_ for _ in ())
        self.read_revision: ReadState = NoRevision()

    def close(self) -> None:
        self.steps.close()

    def invalidate(self) -> None:
        """A changed read stage or completed preparation needs a new read, not a new file."""
        self.read_revision = NoRevision()

    def step(self, service: ObservationService, revision: WireRevision) -> None:
        """Advance one yield; a completed current revision has no work to schedule."""
        try:
            result = next(self.steps)
        except StopIteration:
            self.after_read(service)
            if revision.changed_since(self.view, self.read_revision):
                self.read_revision = revision
                self.steps = self.read(service.comms, revision)
                self.step(service, revision)
        else:
            result.publish(service)
            service.observe_again()

    @abstractmethod
    def read(
        self, comms: Comms, revision: WireRevision,
    ) -> Generator[ObservationStep]: ...

    def after_read(self, service: ObservationService) -> None:
        """Subscribe to completion when this observation has unfinished preparation."""


@final
class ObserverUnattached(RuntimeError, DeclaredError):  # noqa: N818 - nominal reader state
    """A read was handed to an observation process whose reader is not attached yet.

    Attachment is what names the loop that delivers answers, so there is nobody to answer on
    until it has happened.
    """


@final
class ObserverSameLoop(RuntimeError, DeclaredError):  # noqa: N818 - nominal caller state
    """The delivery loop asked itself for a read and would wait for its own answer.

    Every caller today reaches this through a worker thread. A caller on the loop is refused by
    name rather than left waiting, because the wait would never end.
    """


@final
class UnreadableHistory(ValueError, DeclaredError):  # noqa: N818 - nominal stored-read state
    """A strict stored-data read failed, not the observer or the whole UI.

    The reader runtime is known; the writer version is not inferred from a
    missing declaration. Older saved records can fail after a declaration is
    deleted just as newer records can fail in an older reader.
    """

    # Every way Core refuses saved data is a ValueError: StoredHistoryUnreadable and the other
    # relation violations, a codec's refusal of a malformed record, an archive's source checks,
    # and UnreadableHistory itself.
    UNREADABLE_READS = (ValueError,)

    @classmethod
    def outcome[T](cls, read: Callable[[], T]) -> StoredRead[T]:
        """Run a stored read: its value, a read deferred to retry, a reload, or unreadable with
        its cause. A read derives its bounds when it runs, so a reload reads once more now;
        only a read whose own bounds are stale reloads again, and that is its answer."""
        try:
            return cls.attempt(read)
        except ReloadRequired:
            pass
        try:
            return cls.attempt(read)
        except ReloadRequired as error:
            return ReadReload(error)

    @classmethod
    def attempt[T](cls, read: Callable[[], T]) -> StoredRead[T]:
        try:
            return ReadValue(read())
        except DeferredRead as error:
            return ReadDeferred(error)
        except cls.UNREADABLE_READS as error:
            return ReadUnreadable(error, cls.capture(error))

    # Its whole state is its two arguments: the reader runtime and the reason. A remote UI
    # receives them as they are; the reader runtime then names the Core that failed.
    wire_type: ClassVar[object] = tuple[str, str]

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, UnreadableHistory):
            return NotImplemented
        return self.args == other.args

    def __hash__(self) -> int:
        return hash(self.args)

    @classmethod
    def capture(cls, error: Exception) -> UnreadableHistory:
        if isinstance(error, cls):
            return error
        reasons = []
        cause: BaseException | None = error
        while cause is not None:
            reasons.append(str(cause))
            cause = cause.__cause__
        return cls(sys.prefix, "\nCaused by: ".join(reasons))

    def __str__(self) -> str:
        reader_runtime, reason = self.args
        return (f"Saved history unreadable: {reason}\n"
                f"Reader runtime: {reader_runtime}. Restart this process on "
                "the reviewed runtime; if it still fails, the saved records need repair.")


class StoredRead[T](ABC):
    """A stored read's outcome: its value, or one of the three ways it fails."""

    @abstractmethod
    def answer[R](self, value: Callable[[T], R], deferred: Callable[[DeferredRead], R],
                  unreadable: Callable[[UnreadableHistory], R],
                  reload: Callable[[ReloadRequired], R]) -> R: ...

    @abstractmethod
    def raised(self) -> T:
        """The value, or the failure raised again for a caller that answers it itself."""


@dataclass(frozen=True)
class ReadValue[T](StoredRead[T]):
    value: T

    def answer[R](self, value: Callable[[T], R], deferred: Callable[[DeferredRead], R],
                  unreadable: Callable[[UnreadableHistory], R],
                  reload: Callable[[ReloadRequired], R]) -> R:
        return value(self.value)

    def raised(self) -> T:
        return self.value


@dataclass(frozen=True)
class ReadDeferred[T](StoredRead[T]):
    """Currentness or contention: the existing read is retried, not shown as broken."""

    error: DeferredRead

    def answer[R](self, value: Callable[[T], R], deferred: Callable[[DeferredRead], R],
                  unreadable: Callable[[UnreadableHistory], R],
                  reload: Callable[[ReloadRequired], R]) -> R:
        return deferred(self.error)

    def raised(self) -> T:
        raise self.error


@dataclass(frozen=True)
class ReadReload[T](StoredRead[T]):
    """The read's own bounds name a source that is gone: retrying them never answers, so the
    caller re-reads from fresh bounds instead of waiting."""

    error: ReloadRequired

    def answer[R](self, value: Callable[[T], R], deferred: Callable[[DeferredRead], R],
                  unreadable: Callable[[UnreadableHistory], R],
                  reload: Callable[[ReloadRequired], R]) -> R:
        return reload(self.error)

    def raised(self) -> T:
        raise self.error


@dataclass(frozen=True)
class ReadUnreadable[T](StoredRead[T]):
    error: ValueError
    cause: UnreadableHistory

    def answer[R](self, value: Callable[[T], R], deferred: Callable[[DeferredRead], R],
                  unreadable: Callable[[UnreadableHistory], R],
                  reload: Callable[[ReloadRequired], R]) -> R:
        return unreadable(self.cause)

    def raised(self) -> T:
        raise self.cause from self.error


@dataclass(frozen=True)
class AwaitedRead(TaggedRecordMember, ObservationRequest, declared_name=None):
    """A read the UI awaits; the service answers it by ``request_id``.

    ``root`` is the Comms root the UI's view belongs to; a read for another
    root than the observed one is stale, never answered from the wrong stores.
    """

    root: str
    request_id: int = field(default=0, kw_only=True)

    @property
    def refusal_errors(self) -> tuple[type[Exception], ...]:
        return (DeferredRead, ReloadRequired, RelationViolationError, UnreadableHistory)

    def require_root(self, comms: Comms) -> None:
        if Path(self.root).expanduser().resolve() != comms.root.resolve():
            raise ReloadRequired("The read belongs to another Comms root than the observed one")

    def refused(self, error: Exception) -> ReadRefused:
        """Return the read owner's named failure without reclassifying it."""
        return ReadRefused(self.request_id, error, traceback.format_exc())


class ReadRequest(AwaitedRead, declared_name=None):
    """A read of Core's stores, served one at a time in arrival order."""

    deferred_reads: ClassVar[tuple[type[Exception], ...]] = (CoordinationReadUnavailable,)

    def receive(self, service: ObservationService) -> None:
        service.pending[self.request_id] = self
        service.changed.set()

    @abstractmethod
    def read(self, comms: Comms) -> object: ...

    def answer(self, comms: Comms) -> ReadOutcome:
        try:
            self.require_root(comms)
            try:
                value = self.read(comms)
            except self.deferred_reads:
                return ReadBusy(self.request_id)
            return self.answered_value(value)
        except self.refusal_errors as error:
            return self.refused(error)


    def answered_value(self, value: object) -> ReadOutcome:
        """The outcome carrying ``value`` back to the waiter.

        A native read answers as the member declared for its result, so the other side
        decodes it as itself. A read whose result is only known from the request overrides
        this, and inherits every refusal and busy path above unchanged.
        """
        return ReadAnswered.of(self.request_id, value)

    async def answered(self, comms: Comms) -> object:
        """This process answers the read from ``comms`` itself; the outcome settles it the
        way the UI's waiter would, a busy source by asking again."""
        outcome = await Coordination.run_worker(partial(self.answer, comms))
        return await outcome.settled(self, comms)


@dataclass(frozen=True)
class ReadOperation(ReadRequest):
    """The UI's root and request witness around one root-free shared read."""

    operation: ToolRequest

    @property
    def refusal_errors(self) -> tuple[type[Exception], ...]:
        return (*super().refusal_errors, ToolRefusedError, WorkBranchError)

    def read(self, comms: Comms) -> object:
        return UiRead.serving(self.operation).read(comms)


@dataclass(frozen=True)
class CoreReadOperation(ReadRequest):
    """A read the UI asks of Core with the caller it already asks as, answered in the child.

    ReadOperation beside this one serves the native reads, whose implementations declare
    their own caller. This one keeps the caller the UI already uses, so routing a read out
    of the asking interpreter changes where it runs and nothing about what it answers.
    """

    operation: ToolRequest

    @property
    def refusal_errors(self) -> tuple[type[Exception], ...]:
        return (*super().refusal_errors, ToolRefusedError, WorkBranchError)

    def read(self, comms: Comms) -> object:
        # CoreRead admits only the reads it declares, so a mutating request never decodes or
        # runs here; LocalCoreAccess keeps every other request on its own path.
        return CoreRead.serving(self.operation).apply_without_thread(comms, HumanCaller())

    def answered_value(self, value: object) -> ReadOutcome:
        return CoreReadAnswered(self.request_id, FieldCodec.encode(value))

@dataclass(frozen=True)
class OwnerRead(AwaitedRead, declared_name=None):
    """A read scoped to the captured thread incarnation ``owner``.

    Served as it arrives, beside store reads; the shared operation owns its
    answer and decoding. Expected owner loss and refusals reach the caller with
    their original error types, without ending observation or inventing a successful snapshot.
    """

    owner: ThreadIncarnation

    @property
    def refusal_errors(self) -> tuple[type[Exception], ...]:
        return (
            *super().refusal_errors,
            Refused, CoordinationError, ToolRefusedError, ConnectionError, TimeoutError,
        )

    def receive(self, service: ObservationService) -> None:
        service.owner_reads[self.request_id] = service.tasks.create_task(self.serve(service))

    async def serve(self, service: ObservationService) -> None:
        try:
            answer = await self.answer(service.comms)
        finally:
            service.owner_reads.pop(self.request_id, None)
        service.send(answer)

    async def answer(self, comms: Comms) -> ReadOutcome:
        try:
            self.require_root(comms)
            return ReadAnswered.of(self.request_id, await self.ask(comms))
        except self.refusal_errors as error:
            return self.refused(error)

    @abstractmethod
    async def ask(self, comms: Comms) -> object: ...


@dataclass(frozen=True)
class CancelRead(TaggedRecordMember, ObservationRequest):
    """The UI stopped waiting; a read not yet answered is dropped."""

    request_id: int

    def receive(self, service: ObservationService) -> None:
        service.pending.pop(self.request_id, None)
        if (asking := service.owner_reads.pop(self.request_id, None)) is not None:
            asking.cancel()


@dataclass(frozen=True)
class TranscriptRequest[ResultT](ReadRequest):
    """The UI's lifecycle around one shared page read: root, request id, how contention is
    answered and which result it publishes. The page arguments belong to the operation."""

    operation: TranscriptPageRead

    def read(self, comms: Comms) -> ResultT:
        return UnreadableHistory.outcome(lambda: self.result(self.page_read(comms))).raised()

    async def answered(self, comms: Comms) -> ResultT:
        # answer() wraps exactly the value this request's read returned, so the
        # settled answer is this request's result.
        return cast(ResultT, await super().answered(comms))

    def page_read(self, comms: Comms) -> TranscriptRead:
        page = CapturesTranscriptPage.serving(self.operation)
        return page.capture_as(comms, NoDisplaySelection())

    @abstractmethod
    def result(self, read: TranscriptRead) -> ResultT: ...


@dataclass(frozen=True)
class ReadTranscript(TranscriptRequest[TranscriptSnapshotUpdate]):
    """Read a complete page, including its current draft."""

    deferred_reads: ClassVar[tuple[type[Exception], ...]] = (DeferredRead,)

    def result(self, read: TranscriptRead) -> TranscriptSnapshotUpdate:
        return TranscriptSnapshotUpdate.from_read(read)


class SubscribeTranscript(ReadTranscript):
    """A subscription answers contention instead of waiting for another revision."""

    deferred_reads: ClassVar[tuple[type[Exception], ...]] = (StaleRevision,)


class TranscriptPublicationState(TaggedUnion, affix=""):
    """Whether a transcript read was already published; a remote UI sends it with its
    read, so each record carries its kind."""


    @abstractmethod
    def result(self, read: TranscriptRead) -> TranscriptSnapshotUpdate | TranscriptDraftUpdate: ...


@dataclass(frozen=True)
class NotPublished(TaggedRecordMember, TranscriptPublicationState):
    def result(self, read: TranscriptRead) -> TranscriptSnapshotUpdate:
        return TranscriptSnapshotUpdate.from_read(read)


@dataclass(frozen=True)
class Published(TaggedRecordMember, TranscriptPublicationState):
    identity: TranscriptReadIdentity

    def result(self, read: TranscriptRead) -> TranscriptSnapshotUpdate | TranscriptDraftUpdate:
        if self.identity.same_snapshot(read.identity):
            return TranscriptDraftUpdate(read.identity, read.current_draft())
        return TranscriptSnapshotUpdate.from_read(read)


@dataclass(frozen=True)
class ReadTranscriptUpdate(TranscriptRequest[TranscriptSnapshotUpdate | TranscriptDraftUpdate]):
    """The publication state decides whether this read needs a page or its draft."""

    previous: TranscriptPublicationState = field(default_factory=NotPublished)

    def result(self, read: TranscriptRead) -> TranscriptSnapshotUpdate | TranscriptDraftUpdate:
        return self.previous.result(read)


@dataclass(frozen=True)
class RefreshTranscript(ReadRequest):
    """A published page's witness: None while its content is current, else the current page."""

    identity: TranscriptReadIdentity

    def read(self, comms: Comms) -> TranscriptSnapshotUpdate | None:
        identity = self.identity
        read = comms.transcripts.bind_page_read(
            identity.requested_name,
            identity,
            position=identity.position,
            limit=identity.limit,
        )
        if read.content_current():
            return None
        operation = TranscriptPageRead(
            thread=identity.requested_name, position=identity.position, limit=identity.limit,
            page_source=identity.page_source,
        )
        return UnreadableHistory.outcome(lambda: UiRead.serving(operation).read(comms)).raised()


@dataclass(frozen=True)
class ReadNotifications(ReadRequest):
    """The UI's root and request witness around one root-free notification operation."""

    operation: NotificationRead
    display: DisplayCut = field(
        default_factory=NoDisplaySelection, kw_only=True)
    """The painted display this read serves; it decides whose read marks the window omits."""

    def read(self, comms: Comms) -> NotificationWindow:
        return self.operation.apply(comms, self.display)

    @property
    def keys(self) -> tuple[tuple[int, MessageId], ...]:
        """The outcome keys this read shows, as windows key their outcomes."""
        return tuple((reference.seq, reference.message_id) for reference in self.operation.references)

    @property
    def family(self) -> ReadNotifications:
        """This read with its references removed: reads in one family differ only in which
        messages they show, so one family's batches answer each other's references."""
        return self.showing(())

    def showing(self, references: tuple[MessageReference, ...]) -> ReadNotifications:
        return replace(self, operation=replace(self.operation, references=references))


@dataclass(frozen=True)
class ReadOwnerOperation(OwnerRead):
    """The UI's root/recipient witnesses around the shared owner-scoped operation."""

    operation: ToolRequest

    async def ask(self, comms: Comms) -> ToolResult:
        cut = await Coordination.run_worker(comms.registry.store.snapshot_with_authority)
        return await OwnerScopedRead.serving(self.operation).answer(comms, self.owner, cut)


@dataclass(frozen=True)
class ObserveSidebar(InterestRequest):
    worktree: Checkout
    show_stopped: bool
    show_archived: bool

    def observation(self) -> SidebarReading:
        return SidebarReading(self)

    def _answer[T](
        self, revision: WireRevision, read: Callable[[T, RosterVisibility], CoordinationSnapshot], subject: T
    ) -> SidebarResult:

        visibility = RosterVisibility(self.show_stopped, self.show_archived)
        return UnreadableHistory.outcome(lambda: SidebarObserved(
            revision, self, snapshot := read(subject, visibility), SidebarModel.derive(snapshot),
        )).answer(
            lambda observed: observed, lambda error: SidebarPreparing(revision, self, error),
            lambda error: SidebarUnreadable(revision, self, error),
            # This read takes no caller bounds, so no stale witness can reach it; only a
            # source moving under both reads can (TranscriptChanged, DMScopeChanged), and
            # that move is the write the next revision answers.
            lambda error: SidebarPreparing(revision, self, error))


class SidebarReadStage:
    def read(
        self, request: ObserveSidebar, comms: Comms, revision: WireRevision
    ) -> SidebarResult:
        return request._answer(revision, comms.views.viewer_snapshot, request.worktree)

    def advance(self, reading: SidebarReading, snapshot: CoordinationSnapshot) -> None:
        """A full read waits for a changed source or its native-index watcher."""


class RosterSidebarRead(SidebarReadStage):
    def read(
        self, request: ObserveSidebar, comms: Comms, revision: WireRevision
    ) -> SidebarResult:
        viewer = comms.messaging.user_identity(request.worktree).name
        return request._answer(revision, comms.views.presentation.roster, viewer)

    def advance(self, reading: SidebarReading, snapshot: CoordinationSnapshot) -> None:
        reading.stage = SidebarReadStage()
        reading.invalidate()


class SidebarReading(ReadingObservation[ObserveSidebar]):
    view = SidebarView()

    def __init__(self, request: ObserveSidebar):
        super().__init__(request)
        self.stage: SidebarReadStage = RosterSidebarRead()
        self.published: PublishState = NoSidebarResult()
        self.reply_completion: frozenset[Future[ReplyIndexResult]] = frozenset()

    def after_read(self, service: ObservationService) -> None:
        state = service.comms.views.transcript_reads
        loop = asyncio.get_running_loop()
        self.reply_completion = state.watch(
            self.reply_completion, lambda _: loop.call_soon_threadsafe(self.prepared, service)
        )

    def prepared(self, service: ObservationService) -> None:
        service.interest.prepared(self, service)

    def read(self, comms: Comms, revision: WireRevision) -> Generator[ObservationStep]:
        yield from self.publish(self.stage.read(self.request, comms, revision))

    def publish(self, result: SidebarResult) -> Iterator[SidebarResult]:
        result.advance(self)
        if result.accepted_by(self.published):
            self.published = result
            yield result


@dataclass(frozen=True)
class StopSidebar(TaggedRecordMember, ObservationRequest):
    def receive(self, service: ObservationService) -> None:
        service.interest.remove(SidebarReading)


@dataclass(frozen=True)
class StopObserving(TaggedRecordMember, ObservationRequest):
    """Views are retiring; finish their outstanding reads without observing more state."""

    def receive(self, service: ObservationService) -> None:
        service.interest = service.interest.stop()


@dataclass(frozen=True)
class ObserveThreads(InterestRequest):
    # Not ``names``: DeclaredFamily.names is the family's member-name lookup.
    threads: frozenset[str]

    def observation(self) -> ThreadReading:
        return ThreadReading(self)


@dataclass(frozen=True)
class ObserveTeamHud(InterestRequest):
    """The retained team board, prepared on demand independently of heartbeat publication."""

    def observation(self) -> TeamHudReading:
        return TeamHudReading(self)


@dataclass(frozen=True)
class StopTeamHud(TaggedRecordMember, ObservationRequest):
    def receive(self, service: ObservationService) -> None:
        service.interest.remove(TeamHudReading)


class RetainedTeamFrames:
    """Before this reading prepares a board, the page sees the heartbeat's retained frames."""

    def observed(self, comms: Comms, revision: WireRevision) -> Generator[ObservationStep]:
        yield UnreadableHistory.outcome(
            lambda: TeamHudObserved(revision, TeamFrame.read_all(comms.root)),
        ).answer(
            lambda observed: observed,
            lambda error: ObservationPending(),  # Keep the interest until its source can be read.
            lambda error: TeamHudUnreadable(revision, error),
            lambda error: ObservationPending(),  # Bounds-free: only a frame replaced under both reads.
        )


@dataclass(frozen=True)
class PreparedTeamBoard(RetainedTeamFrames):
    """The board this reading derived: the page sees it until the next one is prepared."""

    huds: tuple[TeamHud, ...]

    def observed(self, comms: Comms, revision: WireRevision) -> Generator[ObservationStep]:
        yield TeamHudObserved(revision, self.huds)


class TeamHudReading(TeamBoardRead, ReadingObservation[ObserveTeamHud]):
    """Serve held frames immediately; prepare changed stores outside the service loop.

    The prepared board is this view's own: it is held here and published, never written back
    into the heartbeat's retained frames, which have one writer."""

    view = TeamFramesView()

    def __init__(self, request: ObserveTeamHud):
        super().__init__(request=request)
        self.preparation: set[asyncio.Task[None]] = set()
        self.prepared_revision: ReadState = NoRevision()
        self.board: RetainedTeamFrames = RetainedTeamFrames()

    def close(self) -> None:
        super().close()
        for task in tuple(self.preparation):
            task.cancel()

    def after_read(self, service: ObservationService) -> None:
        if self.preparation or not service.revision.changed_since(
            TeamBoardView(), self.prepared_revision,
        ):
            return
        self.prepared_revision = service.revision
        task = service.tasks.create_task(self.prepare(service))
        self.preparation.add(task)
        task.add_done_callback(self.preparation.discard)

    async def prepare(self, service: ObservationService) -> None:
        """Preparation can retire with the service; it publishes only to this view.

        The board is read through the stored-read owner ``read`` uses: a deferred or reloading
        read prepares again on the next look, an unreadable one reaches the page, and any other
        failure is a defect and raises."""
        board = await service.comms.bus.when_open(
            lambda: self.run_async(partial(
                self.enter_phase, service.comms.root,
                partial(UnreadableHistory.outcome, partial(self.read_board, service.comms)))))
        await board.answer(
            partial(self.publish, service),
            lambda deferred: self.prepare_again(),
            partial(self.unreadable, service),
            lambda reload: self.prepare_again(),
        )

    async def publish(self, service: ObservationService,
                      board: tuple[TeamPlan, tuple[TeamHud, ...]]) -> None:
        self.board = PreparedTeamBoard(board[1])
        service.interest.prepared(self, service)

    async def unreadable(self, service: ObservationService, cause: UnreadableHistory) -> None:
        service.send(TeamHudUnreadable(service.revision, cause))

    async def prepare_again(self) -> None:
        """A read that was only deferred has not prepared this revision."""
        self.prepared_revision = NoRevision()

    def read(self, comms: Comms, revision: WireRevision) -> Generator[ObservationStep]:
        return self.board.observed(comms, revision)


@dataclass(frozen=True)
class ObserveViews(InterestRequest):
    """The thread incarnations and channel views open in the UI, to learn when one is retired."""

    threads: frozenset[ThreadIncarnation]
    channels: frozenset[str]

    def observation(self) -> ViewReading:
        return ViewReading(self)


@dataclass(frozen=True)
class ObserveNotifications(InterestRequest):
    """Original message references currently visible in the open views."""

    reads: frozenset[ReadNotifications]

    def observation(self) -> NotificationReading:
        return NotificationReading(self)


class BatchRead(ABC):
    """What one attempt to read a batch of references produced."""

    @abstractmethod
    def settled(self, family: ReadNotifications, current: list[NotificationBatch],
                pending: set[ReadNotifications]) -> Iterator[ObservationStep]:
        """Record this attempt among the reading's batches, yielding any step it owes now."""


class PendingBatch(BatchRead):
    """The store could not be read yet; the family's requests wait for the next pass."""

    def settled(self, family: ReadNotifications, current: list[NotificationBatch],
                pending: set[ReadNotifications]) -> Iterator[ObservationStep]:
        pending.add(family)
        yield ObservationPending()  # Keep the interest until its source can be read.


@dataclass(frozen=True, eq=False)
class NotificationBatch(BatchRead):
    """One store read of some message references and where it settled. Requests that differ
    only in which references they show share batches: an outcome belongs to its message, not
    to the set of messages that happened to be visible together."""

    request: ReadNotifications
    state: NotificationsRead
    observed: NotificationAnswer

    @cached_property
    def keys(self) -> frozenset[tuple[int, MessageId]]:
        return frozenset(self.request.keys)

    def settled(self, family: ReadNotifications, current: list[NotificationBatch],
                pending: set[ReadNotifications]) -> Iterator[ObservationStep]:
        current.append(self)
        return iter(())


class NotificationReading(ReadingObservation[ObserveNotifications]):
    """Answers each visible-set request from the batches that cover it, and reads only the
    references no current batch covers. Scrolling therefore reads the newly visible rows,
    not every visible row again."""

    view = NotificationsView()

    def __init__(self, request: ObserveNotifications):
        super().__init__(request)
        self.batches: tuple[NotificationBatch, ...] = ()
        self.answered: dict[ReadNotifications, tuple[NotificationBatch, ...]] = {}

    def wanted(self) -> dict[ReadNotifications, frozenset[tuple[int, MessageId]]]:
        """Every key some open request shows, by the request family that reads it."""
        wanted: dict[ReadNotifications, frozenset[tuple[int, MessageId]]] = {}
        for request in self.request.reads:
            wanted[request.family] = wanted.get(request.family, frozenset()) | frozenset(request.keys)
        return wanted

    def continue_from(self, previous: Self) -> Self:
        wanted = self.wanted()
        self.batches = tuple(batch for batch in previous.batches
                             if batch.keys & wanted.get(batch.request.family, frozenset()))
        self.answered = {request: batches for request, batches in previous.answered.items()
                         if request in self.request.reads}
        return self

    def read(self, comms: Comms, revision: WireRevision) -> Generator[ObservationStep]:
        current, stale = [], []
        notifications = NotificationPass(comms, revision)
        for batch in self.batches:
            changed = batch.request.operation.changed_since(comms, batch.state, notifications)
            (stale if changed else current).append(batch)
        pending: set[ReadNotifications] = set()
        for family, keys in self.wanted().items():
            covered = frozenset().union(*(batch.keys for batch in current if batch.request.family == family))
            missing = tuple(dict.fromkeys(
                reference for request in self.request.reads if request.family == family
                for reference in request.operation.references
                if (reference.seq, reference.message_id) not in covered))
            if not missing:
                continue
            known = frozenset().union(*(batch.state.activity.keys() for batch in stale
                                        if batch.request.family == family))
            yield from self.batch(family.showing(missing), comms, revision, known).settled(
                family, current, pending)
        self.batches = tuple(current)
        for request in self.request.reads:
            if request.family not in pending:
                yield from self.answer(request, revision)

    def batch(self, request: ReadNotifications, comms: Comms, revision: WireRevision,
              known: frozenset[str]) -> BatchRead:
        activity = comms.views.observed_activity(revision, known)

        def read() -> NotificationWindow:
            request.require_root(comms)
            return request.read(comms)

        return UnreadableHistory.outcome(read).answer(
            lambda window: NotificationBatch(
                request, NotificationsRead.of(revision, comms, activity, window),
                NotificationsObserved(revision, request, window.outcomes)),
            lambda error: PendingBatch(),
            lambda error: NotificationBatch(
                request, UnreadableNotificationsRead(revision),
                NotificationsUnreadable(revision, request, error)),
            # The request's own display cut is stale: answer it, so the UI asks afresh.
            lambda error: NotificationBatch(
                request, StaleNotificationsRead(revision),
                NotificationsReload(revision, request, error)))

    def answer(self, request: ReadNotifications, revision: WireRevision) -> Iterator[Observed]:
        """Send a request its outcomes only when a batch behind them was read since."""
        keys = frozenset(request.keys)
        sources = tuple(batch for batch in self.batches
                        if batch.request.family == request.family and batch.keys & keys)
        if sources == self.answered.get(request):
            return
        self.answered[request] = sources
        answer: NotificationAnswer = NotificationsObserved(revision, request, {})
        for batch in sources:
            answer = batch.observed.joined(answer)
        yield answer


@dataclass(frozen=True)
class LocatedThreads:
    activity: Mapping[str, ObservedActivity | DeletedThread]
    names: frozenset[str]
    reads: Mapping[str, Callable[[], ThreadPresentation]]


class ThreadReading(ReadingObservation[ObserveThreads]):
    view = StatusView()

    def __init__(self, request: ObserveThreads):
        super().__init__(request)
        self.observed = ThreadsObserved(WireRevision.empty(), {}, frozenset(), {})
        self.initial = request.threads
        self.located_threads = LocatedThreads({}, request.threads, {})
        self.registry_read: RegistrySnapshot | UnreadRegistry = UnreadRegistry()

    def continue_from(self, previous: Self) -> Self:
        self.observed = previous.observed
        self.registry_read = previous.registry_read
        self.initial = self.request.threads - previous.request.threads
        return self

    def read(self, comms: Comms, revision: WireRevision) -> Generator[ObservationStep]:
        previous = self.observed
        results: dict[str, ThreadReadResult] = {}
        outcome = UnreadableHistory.outcome(partial(self.located, comms, revision, previous))
        located = self.located_threads
        yield from outcome.answer(
            lambda located: self.presented(located.reads, results),
            lambda error: self.failed(located.names, ThreadBusy(), results),
            lambda error: self.failed(located.names, ThreadUnavailable(error), results),
            lambda error: self.failed(located.names, ThreadBusy(), results))
        results.update((name, value) for name, value in previous.results.items()
                       if name in self.request.threads and name not in located.names)
        observed = ThreadsObserved(revision, results, frozenset(), located.activity)
        self.observed = observed
        changed = observed.changed_since(previous) | self.initial
        self.initial = frozenset()
        if changed:
            # The reader keeps its acquired results for the next change check; a viewer
            # receives only each changed result's published projection.
            yield replace(observed, changed=changed,
                          results={name: value.published() for name, value in results.items()
                                   if name in changed})

    def located(self, comms: Comms, revision: WireRevision, previous: ThreadsObserved) -> LocatedThreads:
        # Keep each completed stage when a later stored read fails, exactly as
        # the original read did before its activity/names became declared state.
        self.located_threads = LocatedThreads({}, self.request.threads, {})
        readable, _ = previous.revision_inputs()
        names = self.located_threads.names
        watched = names | frozenset(actor for view in readable.values()
                                    for actor in view.notification_window.activity_names())
        activity = comms.views.observed_activity(revision, watched)
        self.located_threads = replace(self.located_threads, activity=activity)
        snapshot = comms.registry.snapshot()
        registry_changed = self.registry_read.changed_names_to(snapshot)
        changed = previous.changed_threads(revision, comms, activity, snapshot, registry_changed)
        self.registry_read = snapshot
        names = (changed | names - previous.results.keys()) & names
        self.located_threads = replace(self.located_threads, names=names)
        if names:
            batch = comms.views.thread_presentations(names)
            self.located_threads = replace(
                self.located_threads, reads={name: partial(batch.read, name) for name in names})
        return self.located_threads

    @staticmethod
    def failed(names: frozenset[str], result: ThreadReadResult,
               results: dict[str, ThreadReadResult]) -> Iterator[ObservationStep]:
        results.update(dict.fromkeys(names, result))
        return iter(())

    @staticmethod
    def presented(reads: Mapping[str, Callable[[], ThreadPresentation]],
                  results: dict[str, ThreadReadResult]) -> Iterator[ObservationStep]:
        for name, read in reads.items():
            results[name] = UnreadableHistory.outcome(read).answer(
                ThreadPresented,
                lambda error: ThreadBusy(), ThreadUnavailable, lambda error: ThreadBusy())
            yield ObservationPending()


class ViewReading(Observation[ObserveViews]):
    def __init__(self, request: ObserveViews):
        super().__init__(request)
        self.revision: ReadState = NoRevision()

    def retire_step(self, service: ObservationService, revision: WireRevision) -> None:
        if not self.revision.changed_since(RegistrationsView(), revision):
            return
        retired = service.comms.views.retired_views(self.request.threads, self.request.channels)
        self.revision = revision
        if retired.threads or retired.channels:
            ViewsRetired(revision, retired).publish(service)


class ServiceResult(TaggedUnion, affix=""):
    """What the service sends; each declares how the UI side receives it. A remote UI
    receives it over the connector, so each record carries its kind."""


    def retire_request(self, service: ObservationService, request: ReadRequest) -> None:
        service.pending.pop(request.request_id)

    @abstractmethod
    async def received(self, process: ObservationProcess, observed: Callable[[Observed], None]) -> None: ...


@dataclass(frozen=True)
class PendingRead:
    """One read in flight: what was asked, and the waiter it answers.

    The request stays beside its waiter so a shared reply is decoded as the result that
    request declares, rather than as a type named by the reply. A reply that arrives after
    its waiter is gone has nothing to resolve against, which is the ignored reply it always
    was.
    """

    asked: AwaitedRead
    waiter: asyncio.Future

class ReadOutcome(TaggedRecordMember, ServiceResult, declared_name=None):
    """The service's answer to a read, including a read that remains busy."""

    @abstractmethod
    async def settled(self, request: ReadRequest, comms: Comms) -> object:
        """The read's result for a caller that answered ``request`` from ``comms`` itself."""


@dataclass(frozen=True)
class Observed(TaggedRecordMember, ObservationStep, ServiceResult, declared_name=None):
    """A result, as of one revision of Core's stores."""

    revision: WireRevision

    def publish(self, service: ObservationService) -> None:
        service.send(self)

    async def received(self, process: ObservationProcess, observed: Callable[[Observed], None]) -> None:
        observed(self)


@dataclass(frozen=True)
class ReadAnswered(ReadOutcome, declared_name=None):
    """A read's answer. Each member carries one declared answer type, so a remote UI decodes
    the answer as itself."""

    members: ClassVar[list[type[ReadAnswered]]] = []
    _answers: ClassVar[dict[type, type[ReadAnswered]]] = {}

    request_id: int
    value: object

    def __init_subclass__(cls, **kwargs: object) -> None:
        super().__init_subclass__(**kwargs)
        ReadAnswered.members.append(cls)
        ReadAnswered._answers = {}

    @staticmethod
    def answers() -> dict[type, type[ReadAnswered]]:
        """Each member answers the type its own ``value`` declares.

        The annotations are resolved here rather than while the class body runs, because this
        module declares its names after the members and a string is not a type.
        """
        if not ReadAnswered._answers:
            declared = {}
            for member in ReadAnswered.members:
                answered = get_type_hints(member)["value"]
                if answered is object:
                    raise TypeError(
                        f"{member.__qualname__} must declare the answer its value carries")
                declared[answered] = member
            ReadAnswered._answers = declared
        return ReadAnswered._answers

    @staticmethod
    def of(request_id: int, value: object) -> ReadAnswered:
        """The boundary: a read's result reaches the wire through its declared answer member."""
        answers = ReadAnswered.answers()
        for kind in type(value).__mro__:
            if kind in answers:
                return answers[kind](request_id, value)
        raise TypeError(f"No read answer is declared for {type(value).__qualname__}")

    async def received(self, process: ObservationProcess, observed: Callable[[Observed], None]) -> None:
        if (pending := process.reads.pop(self.request_id, None)) is not None and not pending.waiter.done():
            pending.waiter.set_result(self.value)

    async def settled(self, request: ReadRequest, comms: Comms) -> object:
        return self.value


@dataclass(frozen=True)
class NothingAnswered(ReadAnswered):
    value: None = None


@dataclass(frozen=True)
class TranscriptAnswered(ReadAnswered):
    value: TranscriptSnapshotUpdate


@dataclass(frozen=True)
class DraftAnswered(ReadAnswered):
    value: TranscriptDraftUpdate


@dataclass(frozen=True)
class NotificationsAnswered(ReadAnswered):
    value: NotificationWindow


@dataclass(frozen=True)
class TeammateAnswered(ReadAnswered):
    value: TeammateDetail


@dataclass(frozen=True)
class GoalAnswered(ReadAnswered):
    value: GoalSnapshotReply


@dataclass(frozen=True)
class DeliveryAnswered(ReadAnswered):
    value: DeliveryOverview





@dataclass(frozen=True)
class CoreReadAnswered(ReadOutcome):
    """The one reply for a shared Core read, carrying its result as the wire value.

    A native read has a member per result so the other side can decode it from the reply
    alone. A shared Core read does not need one: the waiter still holds the request it
    sent, and that request declares its result, so this reply carries the value and the
    request names the type. Twenty-one members would say the same thing twenty-one times.
    """

    request_id: int
    wire: object

    async def received(
        self, process: ObservationProcess, observed: Callable[[Observed], None],
    ) -> None:
        pending = process.reads.get(self.request_id)
        if pending is None or pending.waiter.done():
            return  # Cancelled, retired, or already answered: the same ignored reply as ever.
        # The waiter stays owned until it is settled. Popping first and then decoding leaves a
        # thread waiting on a future nobody holds any more: a malformed reply ends the reader,
        # and retirement cannot settle a waiter it can no longer find.
        try:
            value = FieldCodec.decode(pending.asked.operation.result_type(), self.wire)
        except Exception as undecodable:
            process.reads.pop(self.request_id, None)
            pending.waiter.set_exception(undecodable)
            raise
        process.reads.pop(self.request_id, None)
        pending.waiter.set_result(value)

    async def settled(self, request: ReadRequest, comms: Comms) -> object:
        return FieldCodec.decode(request.operation.result_type(), self.wire)

@dataclass(frozen=True)
class ReadBusy(ReadOutcome):
    """Source busy: the original read remains pending, not a failed input."""

    request_id: int
    # SQLite announces no lock release, so the service tries the read again after this pause.
    retry_seconds: ClassVar[float] = 0.2

    def retire_request(self, service: ObservationService, request: ReadRequest) -> None:
        service.busy.set()

    @property
    def title(self) -> str:
        return "History loading"

    async def received(self, process: ObservationProcess, observed: Callable[[Observed], None]) -> None:
        # Keep the same waiter. The service owns waiting for the source and
        # answers this request when its next observation can acquire a read.
        pass

    async def settled(self, request: ReadRequest, comms: Comms) -> object:
        await asyncio.sleep(self.retry_seconds)
        return await request.answered(comms)


@dataclass(frozen=True)
class ReadRefused(ReadOutcome):
    """The read raised ``error``; the UI caller receives that same exception."""

    request_id: int
    error: Exception
    trace: str

    async def received(self, process: ObservationProcess, observed: Callable[[Observed], None]) -> None:
        if (pending := process.reads.pop(self.request_id, None)) is not None and not pending.waiter.done():
            self.error.add_note(f"Raised in the comms observation service:\n{self.trace}")
            pending.waiter.set_exception(self.error)

    async def settled(self, request: ReadRequest, comms: Comms) -> object:
        raise self.error


@dataclass(frozen=True)
class RevisionObserved(Observed):
    """Core's stores changed; consumers that only need to know react to this."""


class PublishState:
    def same_observation(self, current: object) -> bool:
        return self == current

    def accepts_preparing(self) -> bool:
        return False


class NoSidebarResult(PublishState):
    def accepts_preparing(self) -> bool:
        return True


@dataclass(frozen=True)
class SidebarResult(PublishState, Observed, declared_name=None):
    revision: WireRevision = field(compare=False)
    request: ObserveSidebar

    def advance(self, reading: SidebarReading) -> None:
        """Keep this stage until its source revision changes."""

    def accepted_by(self, previous: PublishState) -> bool:
        return not previous.same_observation(self)


@dataclass(frozen=True)
class SidebarPreparing(SidebarResult, WaitingSidebar):
    """The first answer explains the unfinished read without replacing real rows."""

    cause: DeferredRead | ReloadRequired

    def accepted_by(self, previous: PublishState) -> bool:
        return previous.accepts_preparing()

    def status_text(self) -> str:
        return f"{super().status_text()}\n{self.cause}"


@dataclass(frozen=True)
class SidebarObserved(SidebarResult):
    snapshot: CoordinationSnapshot
    derived: tuple[dict[str, ChannelRowModel], dict[str, ThreadRowModel]]

    def advance(self, reading: SidebarReading) -> None:
        reading.stage.advance(reading, self.snapshot)


@dataclass(frozen=True)
class SidebarUnreadable(SidebarResult):
    """The requested sidebar could not be read; no stale snapshot is offered."""

    error: UnreadableHistory


class ThreadReadResult(PublishState, TaggedUnion, affix=""):
    """One thread's last read, including how to compare and display its answer. A remote
    UI receives it across the connector, so each record carries its kind."""


    def same_as_presented(self, presented: ThreadPresented) -> bool:
        return False

    def published(self) -> ThreadReadResult:
        """What a viewer receives of this read: the read itself."""
        return self

    @abstractmethod
    def status_row(self, name: str, owner: OpenThreadStatus) -> ThreadStatusModel: ...

    def collect_revision_inputs(self, name: str, readable: dict[str, AcquiredThreadPresentation],
                                retry: set[str]) -> None:
        """Supply readable values and reads that must be tried again."""

    def same_status(self, presentation: ThreadPresentation) -> bool:
        return False


@dataclass(frozen=True)
class ThreadPresented(TaggedRecordMember, ThreadReadResult):
    presentation: ThreadPresentation

    def same_observation(self, previous: ThreadReadResult) -> bool:
        return previous.same_as_presented(self)

    def same_as_presented(self, presented: ThreadPresented) -> bool:
        return (presented.presentation is self.presentation
                or presented.presentation.same_observation(self.presentation))

    def published(self) -> ThreadPresented:
        return ThreadPresented(self.presentation.published())

    def status_row(self, name: str, owner: OpenThreadStatus) -> ThreadStatusModel:
        return ThreadStatusModel.known(name, self.presentation)

    def collect_revision_inputs(self, name: str, readable: dict[str, AcquiredThreadPresentation],
                                retry: set[str]) -> None:
        self.presentation.collect_revision_input(name, readable)

    def same_status(self, presentation: ThreadPresentation) -> bool:
        return presentation.same_status(self.presentation)


@dataclass(frozen=True)
class ThreadUnavailable(TaggedRecordMember, ThreadReadResult):
    error: UnreadableHistory

    def status_row(self, name: str, owner: OpenThreadStatus) -> ThreadStatusModel:
        return ThreadStatusModel.unreadable(name, self.error)


@dataclass(frozen=True)
class ThreadBusy(TaggedRecordMember, ThreadReadResult):
    def status_row(self, name: str, owner: OpenThreadStatus) -> ThreadStatusModel:
        return owner.open(name)

    def collect_revision_inputs(self, name: str, readable: dict[str, AcquiredThreadPresentation],
                                retry: set[str]) -> None:
        retry.add(name)


@dataclass(frozen=True)
class ThreadsObserved(Observed):
    """One mutually exclusive read answer per observed thread."""

    results: Mapping[str, ThreadReadResult]
    changed: frozenset[str]
    activity: Mapping[str, ObservedActivity | DeletedThread]

    def changed_threads(self, revision: WireRevision, comms: Comms,
                        activity: Mapping[str, ObservedActivity | DeletedThread],
                        snapshot: RegistrySnapshot, registry_changed: frozenset[str],
                        ) -> frozenset[str]:
        """Attribute changes using the retained observation and existing route rows. A
        registry revision reaches only the threads whose own rows it changed (or their forks):
        every turn start rewrites the registry, and it re-presented every open thread."""
        earlier = self.revision
        names = self.results.keys()
        readable, changed = self.revision_inputs()
        if revision.changed_since(BusSourceView(), earlier) or not readable:
            return frozenset(names)
        changed.update(name for name in names if snapshot.affected_by(name, registry_changed))
        if revision.bus_seq != earlier.bus_seq:
            source = comms.bus.bus
            if source.revision() != revision.bus_seq:
                raise StaleRevision("Bus changed before route attribution")
            actors = source.changed_names(earlier.bus_seq)
            catalog = comms.channels.catalog.read()
            with comms.bus.opened_activity() as channel_activity:
                for actor in actors:
                    changed.update(CommandTarget.named(actor).changed_threads(
                        names, snapshot, catalog, channel_activity))
            changed.update(alias for alias, target in snapshot.aliases.items() if target in changed)
        activity_changed = {name for name in activity
                            if activity[name] != self.activity.get(name)}
        status_names = names if (revision.goal_execution, revision.expiry_period, revision.work_room) != (
            earlier.goal_execution, earlier.expiry_period, earlier.work_room) else names & activity_changed
        if status_names:
            status_batch = comms.views.thread_presentations(status_names)
            changed.update(name for name in status_names
                           if not self.results[name].same_status(status_batch.view(name)))
        remaining = {name: view for name, view in readable.items() if name not in changed}
        notifications = NotificationPass(comms, revision)
        changed.update(name for name, view in remaining.items()
                       if view.notification_window.activity_names() & activity_changed
                       or notifications.changed(view.notification_window, earlier))
        return frozenset(changed & names)

    def revision_inputs(self) -> tuple[dict[str, AcquiredThreadPresentation], set[str]]:
        readable: dict[str, AcquiredThreadPresentation] = {}
        retry: set[str] = set()
        for name, result in self.results.items():
            result.collect_revision_inputs(name, readable, retry)
        return readable, retry

    def changed_since(self, previous: ThreadsObserved) -> frozenset[str]:
        """Read-state transitions and source-owned changes, independent of store churn."""
        return frozenset(name for name in self.results.keys() | previous.results.keys()
                         if name not in self.results or name not in previous.results
                         or not self.results[name].same_observation(previous.results[name]))


@dataclass(frozen=True)
class NotificationAnswer(Observed, Fold):
    """What one notification read answers its request: its outcomes, or why it could not read.
    A request's answer folds the answers of the batches behind it."""

    request: ReadNotifications


@dataclass(frozen=True)
class NotificationsObserved(Accumulates, NotificationAnswer):
    outcomes: Mapping[tuple[int, MessageId], tuple[MessageNotification, ...]]

    def including(self, batch: NotificationsObserved) -> NotificationsObserved:
        """This request's answer with a readable batch's outcomes for the keys it shows."""
        keys = frozenset(self.request.keys)
        return replace(self, outcomes={**self.outcomes, **{
            key: rows for key, rows in batch.outcomes.items() if key in keys}})


@dataclass(frozen=True)
class NotificationsReload(Absorbs, NotificationAnswer):
    """The request's display cut names a source that is gone; the UI reloads its page."""

    error: ReloadRequired


@dataclass(frozen=True)
class NotificationsUnreadable(Absorbs, NotificationAnswer):
    error: UnreadableHistory

    def absorbing(self, answer: NotificationAnswer) -> NotificationsUnreadable:
        """One unreadable batch makes the whole answer unreadable, for the answer's request."""
        return NotificationsUnreadable(answer.revision, answer.request, self.error)


@dataclass(frozen=True)
class TeamHudObserved(Observed):
    """Every beating teammate's newest frame, by teammate name."""

    huds: tuple[TeamHud, ...]


@dataclass(frozen=True)
class TeamHudUnreadable(Observed):
    """The newest teammate frame cannot be read; retain the last good frame."""

    error: UnreadableHistory


@dataclass(frozen=True)
class ViewsRetired(Observed):
    """Open views whose thread was deleted or replaced, or whose channel is gone or archived."""

    retired: RetiredViews


@dataclass(frozen=True)
class ObservationFailed(TaggedRecordMember, ServiceResult):
    """The service failed and is exiting; ``error`` is its traceback."""

    error: str

    async def received(self, process: ObservationProcess, observed: Callable[[Observed], None]) -> None:
        await Coordination.run_worker(partial(process.peer.await_end, 2))
        raise process.ended(ServiceReported(self.error))


class ObserverStatus(ABC):
    """The reported observer outcome owns whether a request can proceed."""

    @abstractmethod
    def raise_if_failed(self) -> None: ...

    def failure(self, create: Callable[[], ObserverDied]) -> ObserverDied:
        return create()

    def retire(self, process: ObservationProcess) -> None:
        pass


class NoObserverFailure(ObserverStatus):
    def raise_if_failed(self) -> None:
        return None

    def retire(self, process: ObservationProcess) -> None:
        retired = ObserverRetired("The Comms observation retired before the read was answered")
        process.status = retired
        process.abandon_reads(retired)


class ObservationEnded(CoordinationError):
    """The observation is over, so a request to it cannot be answered.

    The one base both terminal outcomes share: `ObserverRetired` for a process already closed,
    `ObserverDied` (and the connector's `ConnectorObserverClosed`) for one that ended while the
    request was being sent. A caller ending the observation itself is entering this state, not
    failing at it, so it catches this and nothing wider — `CoordinationError` is the whole error
    family, and `ObserverReadFailed` re-raises an arbitrary cause through the same method.
    """


class ObserverRetired(ReadAgain, ObservationEnded, ObserverStatus):
    """This observation process closed and never answers again; the read is dropped, and the
    owner's replacement process reads afresh. Retrying here never answers: no DeferredRead."""

    def raise_if_failed(self) -> None:
        raise self


@dataclass(frozen=True)
class ObserverReadFailed(ObserverStatus):
    error: BaseException

    def raise_if_failed(self) -> None:
        raise self.error

    def settle(self, process: ObservationProcess) -> None:
        process.status = self
        process.abandon_reads(self.error)


class ObserverDied(ObservationEnded, ObserverStatus):
    """The observation service ended; reads share its reported cause.

    Presentation and recovery belong to the consuming application; each member says how it
    ended.
    """

    def raise_if_failed(self) -> None:
        raise self

    def failure(self, create: Callable[[], ObserverDied]) -> ObserverDied:
        return self


class ObserverProcessDied(ObserverDied):
    """The local observation process ended; it reports its own pid and how it ended."""

    def __init__(self, process: BaseProcess, error: str = ""):
        self.process, self.error = process, error
        super().__init__(f"The comms observer (pid {process.pid}) {self.ending(process)}"
                         + (f":\n{error}" if error else ""))

    @staticmethod
    def ending(process: BaseProcess) -> str:
        """multiprocessing's exit code, decoded once: a signal, a status, or still running."""
        match process.exitcode:
            case int(status) if status < 0:
                return f"was killed by signal {-status} ({signal.strsignal(-status)})"
            case int(status):
                return f"exited with status {status}"
            case _:
                return "closed its pipe and has not exited"


class ObservationService:
    """The child process side: one Comms service, the UI's interest, and what was sent."""

    def __init__(self, comms, connection: Connection):
        self.comms = comms
        self.connection = connection
        self.interest: ObservationInterest = LiveObservationInterest()
        self.changed = asyncio.Event()
        # A read answered busy, until the service tries the pending reads again.
        self.busy = asyncio.Event()
        self.revision: ReadState = NoRevision()
        self.observe_due = True
        # Store reads the UI awaits, in arrival order, until served or cancelled.
        self.pending: dict[int, ReadRequest] = {}
        # Owner reads being asked, until answered or cancelled.
        self.owner_reads: dict[int, asyncio.Future] = {}
        self.tasks = asyncio.TaskGroup()
        self.closed = False

    def retire(self) -> None:
        """Either pipe direction ending retires this service's outstanding work once."""
        if self.closed:
            return
        self.closed = True
        self.interest = self.interest.stop()
        for asking in tuple(self.owner_reads.values()):
            asking.cancel()
        self.changed.set()

    def send(self, result: ServiceResult) -> None:
        """Every ordinary reply shares the peer's lifetime, not just received EOF."""
        if self.closed:
            return
        try:
            self.connection.send(result)
        except BrokenPipeError:
            self.retire()

    def receive(self) -> None:
        if self.closed:
            return
        while self.connection.poll():
            try:
                request = self.connection.recv()
            except (EOFError, ConnectionResetError):
                self.retire()
                return
            request.receive(self)

    async def watch(self) -> None:
        sources = tuple(FileSource(path)
                        for group in WireRevision.source_paths(self.comms.views).values()
                        for path in group)
        async with aclosing(self.comms.bus.bus.watch(*sources)) as changes:
            async for _ in changes:
                self.observe_again()

    def observe_again(self) -> None:
        if not self.closed:
            self.observe_due = True
            self.changed.set()

    async def answer_pending(self, attempted: set[int]) -> None:
        """Finish each waiting store read once, accepting cancellations between reads."""
        await asyncio.sleep(0)
        self.receive()
        for request_id, request in tuple(self.pending.items()):
            if self.closed:
                return
            if request_id in attempted or self.pending.get(request_id) is not request:
                continue
            attempted.add(request_id)
            answer = request.answer(self.comms)
            self.send(answer)
            answer.retire_request(self, request)
            await asyncio.sleep(0)

    async def observe(self, attempted: set[int]) -> None:
        revision = self.comms.views.revision()
        if self.revision.changed_since(StoresView(), revision):
            self.send(RevisionObserved(revision))
        self.revision = revision
        await self.interest.observe(self, revision, attempted)

    async def expire(self) -> None:
        """Time-based sidebar state belongs to the sidebar observer, not file watching."""
        while not self.closed:
            now = time.time()
            await asyncio.sleep(WireRevision.expiry_seconds - now % WireRevision.expiry_seconds)
            self.observe_again()

    async def retry_busy(self) -> None:
        """A busy source frees without a commit, so no watched source changes."""
        while not self.closed:
            await self.busy.wait()
            self.busy.clear()
            await asyncio.sleep(ReadBusy.retry_seconds)
            self.changed.set()

    async def run(self) -> None:
        loop = asyncio.get_running_loop()
        # Request and watcher defects belong to the service process boundary;
        # an unobserved background-task error must not strand an awaited UI read.
        async with self.tasks:
            loop.add_reader(self.connection.fileno(), self.receive)
            watching = self.tasks.create_task(self.watch())
            expiring = self.tasks.create_task(self.expire())
            retrying = self.tasks.create_task(self.retry_busy())
            try:
                while not self.closed:
                    await self.changed.wait()
                    self.changed.clear()
                    attempted: set[int] = set()
                    await self.answer_pending(attempted)
                    if self.observe_due and not self.closed:
                        self.observe_due = False
                        await self.observe(attempted)
            finally:
                self.retire()
                loop.remove_reader(self.connection.fileno())
                watching.cancel()
                expiring.cancel()
                retrying.cancel()
                self.pending.clear()
                self.comms.views.transcript_reads.close()


class RelayConnection(Connection):
    """One side of a relay socket. A relay blocks a worker thread in recv_bytes; closing
    the descriptor from another thread does not wake it, so close shuts the socket down
    first, which ends that read with EOF. A write the other side never takes fails after
    write_timeout_seconds, so a reader that stopped ends the relay instead of pinning it
    and its threads forever."""

    write_timeout_seconds: ClassVar[int] = 30

    def __init__(self, handle: int, readable: bool = True, writable: bool = True) -> None:
        super().__init__(handle, readable, writable)
        with socket.socket(fileno=os.dup(handle)) as peer:
            peer.setsockopt(socket.SOL_SOCKET, socket.SO_SNDTIMEO,
                            struct.pack("ll", self.write_timeout_seconds, 0))

    def close(self) -> None:
        if not self.closed:
            with socket.socket(fileno=os.dup(self.fileno())) as peer:
                peer.shutdown(socket.SHUT_RDWR)
        super().close()


class RelayEnding(ABC):
    """How a relay between a local connection and a WebSocket ended; the observation it
    carried reports it."""

    @abstractmethod
    def describe(self) -> str: ...


@dataclass(frozen=True)
class LocalSideEnded(RelayEnding):
    """The local socket reached its end of stream."""

    error: BaseException

    def describe(self) -> str:
        return f"the local side ended ({type(self.error).__name__})"


@dataclass(frozen=True)
class LocalSideStoppedReading(RelayEnding):
    """A write to the local socket passed its deadline: that side stopped reading."""

    error: BaseException

    def describe(self) -> str:
        return f"the local side stopped reading ({type(self.error).__name__})"


@dataclass(frozen=True)
class PeerSentFrame(RelayEnding):
    """The peer sent a frame that is not a record."""

    kind: str

    def describe(self) -> str:
        return f"the peer sent a {self.kind} frame"


@dataclass(frozen=True)
class PeerClosed(RelayEnding):
    def describe(self) -> str:
        return "the peer closed its socket"


async def relay_frames(connection: Connection, peer: WebSocketFrames) -> RelayEnding:
    """Carry frames both ways between a local connection and a WebSocket until either side
    ends, then close the WebSocket, stop both directions and say which side ended first.

    The local connection stays open: its owner records the returned reason and then closes
    it, which also wakes a worker thread still blocked on it. Socket reads and writes run in
    worker threads, so a peer that stops reading never blocks this loop. A direction that
    fails in any other way raises its own error here."""
    ended: asyncio.Future[RelayEnding] = asyncio.get_running_loop().create_future()

    def end(ending: RelayEnding) -> None:
        """The first side to end names the relay's end; a later one only follows it."""
        if not ended.done():
            ended.set_result(ending)

    async def outward() -> None:
        try:
            while True:
                await peer.send_bytes(await asyncio.to_thread(connection.recv_bytes))
        except (EOFError, OSError) as error:
            # The local socket's own end-of-stream: this is how its side ends.
            end(LocalSideEnded(error))

    async def inward() -> None:
        try:
            async for message in peer:
                if message.type is not WSMsgType.BINARY:
                    end(PeerSentFrame(message.type.name))
                    return
                await asyncio.to_thread(connection.send_bytes, message.data)
        except OSError as error:
            # The local socket's write deadline: the local side stopped reading.
            end(LocalSideStoppedReading(error))
            return
        end(PeerClosed())

    directions = (asyncio.create_task(outward()), asyncio.create_task(inward()))
    try:
        await asyncio.wait((ended, *directions), return_when=asyncio.FIRST_COMPLETED)
    finally:
        for direction in directions:
            direction.cancel()
        await peer.close()
        await asyncio.wait(directions)
    for direction in directions:
        # A direction that failed rather than ended re-raises its own error here.
        direction.cancelled() or direction.result()
    return ended.result()


class EncodedConnection(Connection):
    """The local pipe's framing and lifetime, carrying declared records instead of pickles,
    so an observation can be served across a boundary the peer does not trust with code.
    Each end declares what it sends and what it receives."""

    sends: ClassVar[type]
    receives: ClassVar[type]

    def send(self, obj: object) -> None:
        self.send_bytes(FieldCodec.compact(FieldCodec.encode_document(obj, self.sends)).encode())

    def recv(self) -> object:
        return self.decode(self.recv_bytes())

    @classmethod
    def decode(cls, packet: bytes) -> object:
        """Decode through the receiving end's own record declaration."""
        return FieldCodec.decode_document(cls.receives, FieldCodec.parse(packet.decode()))


class ServiceEnd(EncodedConnection):
    """The observation service's end: results out, requests in."""

    sends = ServiceResult
    receives = ObservationRequest


class ViewerEnd(EncodedConnection):
    """A UI's end: requests out, results in."""

    sends = ObservationRequest
    receives = ServiceResult


def serve_encoded(route: CommsRoute, service_socket: socket.socket) -> None:
    """Serve one remote viewer over its socket, decoding only declared records."""
    serve(route, ServiceEnd(service_socket.detach()))


def serve(route: CommsRoute, connection: Connection) -> None:
    """Observe the caller's selected route, including its reviewed native package."""
    started()
    try:
        asyncio.run(ObservationService(Comms(route), connection).run())
    except BaseException:
        try:
            connection.send(ObservationFailed(traceback.format_exc()))
        except BrokenPipeError:
            pass  # The reader has gone; preserve the original service failure.
        raise
    finally:
        connection.close()


class ObservationSource(ABC):
    """Something a UI can observe Core through: it supplies the observation's peer."""

    @abstractmethod
    def observation_peer(self) -> ObservationPeer: ...


@dataclass(frozen=True)
class LocalRouteObservation(ObservationSource):
    """A route observed in a local process on its root, started the way ``start`` says."""

    route: CommsRoute
    start: HelperStart = field(default_factory=SpawnedHelper)

    def observation_peer(self) -> LocalObservationPeer:
        return LocalObservationPeer(self.route, self.start)


class ObserverEnd(ABC):
    """How a UI learned its observation ended. The peer that served it says what that means:
    a local process reports its exit, a connector its relay's recorded end."""

    @abstractmethod
    def reported_by(self, peer: ObservationPeer) -> ObserverDied: ...


@dataclass(frozen=True)
class ServiceReported(ObserverEnd):
    """The service sent its own traceback before exiting: that is the cause."""

    traceback: str

    def reported_by(self, peer: ObservationPeer) -> ObserverDied:
        return peer.service_failed(self.traceback)


@dataclass(frozen=True)
class ConnectionClosed(ObserverEnd):
    """The connection reached its end without a report from the service."""

    def reported_by(self, peer: ObservationPeer) -> ObserverDied:
        return peer.connection_closed()


@dataclass(frozen=True)
class ConnectionBroken(ObserverEnd):
    """A request found the connection already gone."""

    error: str

    def reported_by(self, peer: ObservationPeer) -> ObserverDied:
        return peer.connection_broken(self.error)


class ObservationPeer(ABC):
    """Who serves a UI's observation: a local process, or a Core across the connector.
    It owns the connection the UI reads, how the service is joined, and how its end reads."""

    connection: Connection

    @abstractmethod
    def join(self) -> None:
        """Wait for the service to end; never ends it."""

    @abstractmethod
    def await_end(self, seconds: float) -> None:
        """Wait up to ``seconds`` for the service to end; never ends it."""

    def retire(self) -> None:
        """End this observation's service and wait for it."""
        self.join()

    def died(self, end: ObserverEnd) -> ObserverDied:
        return end.reported_by(self)

    @abstractmethod
    def service_failed(self, traceback: str) -> ObserverDied: ...

    @abstractmethod
    def connection_closed(self) -> ObserverDied: ...

    def connection_broken(self, error: str) -> ObserverDied:
        """A failed request shows only that the connection is gone: by default, its closing."""
        return self.connection_closed()


class LocalObservationPeer(ObservationPeer):
    """The service in a spawned process of this machine, over a pipe."""

    def __init__(self, route: CommsRoute, start: HelperStart) -> None:
        context = start.context()
        self.connection, child = context.Pipe()
        with ExitStack() as acquisition:
            acquisition.enter_context(self.connection)
            with child:
                self.process = context.Process(target=serve, args=(route, child), daemon=False,
                                               name="agent-comms-observation")
                self.process.start()
            acquisition.pop_all()

    def join(self) -> None:
        self.process.join()

    def await_end(self, seconds: float) -> None:
        self.process.join(seconds)

    def service_failed(self, traceback: str) -> ObserverDied:
        return ObserverProcessDied(self.process, traceback)

    def connection_closed(self) -> ObserverDied:
        return ObserverProcessDied(self.process)

    def connection_broken(self, error: str) -> ObserverDied:
        return ObserverProcessDied(self.process, error)


class ObservationProcess(AbstractAsyncContextManager, MroDispatch):
    """One ordered receive/decode task, loop-owned delivery, and joined retirement."""

    @classmethod
    def awaited(cls, route: CommsRoute) -> Self:
        process = cls(LocalRouteObservation(route))
        process.start_receiving(process.observed, process.failed)
        return process

    def __init__(self, source: ObservationSource):
        """``source`` names who serves the observation: a route locally, a remote UI profile
        across the connector."""
        self._resources = ExitStack()
        self._delivery: asyncio.AbstractEventLoop | None = None
        self.request_ids = itertools.count(1)
        self.reads: dict[int, PendingRead] = {}
        self.status: ObserverStatus = NoObserverFailure()
        self.peer = source.observation_peer()
        self.connection = self.peer.connection
        self._resources.callback(self.peer.retire)
        self._resources.enter_context(self.connection)
        self._reader = AsyncExitStack()

    async def __aexit__(self, *exc: object) -> None:
        await self.close()

    def start_receiving(
        self, observed: Callable[[Observed], None], failed: Callable[[Exception], None],
    ) -> None:
        """Start this process's reader on the delivery loop, once at attachment.

        The loop is taken here rather than in ``__init__``: a process is built wherever its
        owner happens to run, including a worker thread, and only attachment runs on the loop
        that will deliver its answers.
        """
        self._delivery = asyncio.get_running_loop()
        receiving = asyncio.create_task(self.receive(observed, failed))
        receiving.add_done_callback(partial(self.receive_ended, failed))
        self._reader.push_async_callback(self.stop_receiving, receiving)

    def receive_ended(
        self, failed: Callable[[Exception], None], receiving: asyncio.Task[None],
    ) -> None:
        if not receiving.cancelled():
            self.dispatch_sync(receiving.exception(), failed)

    @handles(Exception)
    def receive_error(self, error: Exception, failed: Callable[[Exception], None]) -> object:
        ObserverReadFailed(error).settle(self)
        failed(error)
        return self.DISCARD

    @handles(BaseException)
    def receive_interrupted(
        self, error: BaseException, failed: Callable[[Exception], None],
    ) -> None:
        ObserverReadFailed(error).settle(self)
        raise error

    async def stop_receiving(self, receiving: asyncio.Task[None]) -> None:
        receiving.cancel()
        await asyncio.gather(receiving, return_exceptions=True)

    def observed(self, result: Observed) -> None:
        """Awaited-only consumers have no view updates to apply."""

    def failed(self, error: Exception) -> None:
        """The status and pending reads already carry this failure."""

    def stop_observing(self) -> None:
        """Tell the peer to stop observing; an observation that ends while saying so has ended.

        The goodbye is a courtesy, not a precondition: a peer that already dropped the
        connection makes `request` raise its own terminal outcome, which is the state this
        method is entering. Catching `ObservationEnded` here keeps that answer with the process
        that owns it instead of raising it at every caller that ends an observation.
        """
        try:
            self.request(StopObserving())
        except ObservationEnded:
            pass

    def request(self, request: ObservationRequest) -> None:
        """Tell the service what the UI shows, or report its end to this caller."""
        self.status.raise_if_failed()
        if self.connection.closed:
            raise ObserverRetired("Cannot request a retired Comms observation process")
        try:
            self.connection.send(request)
        except BrokenPipeError as error:
            died = self.ended(ConnectionBroken(str(error)))
        else:
            return
        # The pickler's failed-send traceback retains an exported BytesIO
        # view. ObserverDied carries the cause text, not that pipe buffer.
        raise died

    def ended(self, end: ObserverEnd) -> ObserverDied:
        """The service ended (``error``: the traceback it sent): fail its awaited reads and say how it exited."""
        died = self.status.failure(
            partial(self.peer.died, end)
        )
        self.status = died
        self.abandon_reads(died)
        return died

    def read_from_thread(self, request: AwaitedRead):
        """Answer ``request`` for a caller on another thread, on this process's own loop.

        The loop stays private: a caller hands over the request and waits for the answer the
        delivery loop settles, so read ids, cancellation, retirement and refusals keep their
        existing owners. A caller already on that loop would be waiting for itself, so it is
        refused by name instead.
        """
        delivery = self._delivery
        if delivery is None:
            raise ObserverUnattached("This observation process has not attached its reader yet")
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if running is delivery:
            raise ObserverSameLoop(
                "A read from the delivery loop itself would wait for its own answer")
        return asyncio.run_coroutine_threadsafe(self.read(request), delivery).result()

    async def read(self, request: AwaitedRead):
        """Send ``request`` and await its loop-delivered answer.

        Cancelling the caller cancels the read; an unserved read is dropped.
        """
        request = replace(request, request_id=next(self.request_ids))
        waiter = asyncio.get_running_loop().create_future()
        self.reads[request.request_id] = PendingRead(request, waiter)
        try:
            self.request(request)
            return await waiter
        except asyncio.CancelledError:
            if self.reads.pop(request.request_id, None) is not None:
                self.request(CancelRead(request.request_id))
            raise
        finally:
            self.reads.pop(request.request_id, None)
            # A send can fail before this coroutine awaits its future. Settle
            # that local waiter without losing the request's ObserverDied.
            if not waiter.done():
                waiter.cancel()
            elif not waiter.cancelled():
                waiter.exception()

    def abandon_reads(self, error: BaseException) -> None:
        """The reader's retirement settles its waiters with the original cause."""
        reads, self.reads = self.reads, {}
        for pending in reads.values():
            if not pending.waiter.done():
                pending.waiter.set_exception(error)

    async def readable(self) -> None:
        """Wait until one packet can be received by the worker."""
        loop = asyncio.get_running_loop()
        descriptor = self.connection.fileno()
        ready: asyncio.Future[None] = loop.create_future()

        def available() -> None:
            loop.remove_reader(descriptor)
            if not ready.done():
                ready.set_result(None)

        loop.add_reader(descriptor, available)
        try:
            await ready
        finally:
            loop.remove_reader(descriptor)

    async def receive(
        self, observed: Callable[[Observed], None], failed: Callable[[Exception], None],
    ) -> None:
        """Read and unpickle one complete packet off-loop; settle it on this loop.

        Start the next read only after its predecessor is delivered.
        Retirement joins a started read even when cancelled partway through.
        """
        try:
            while True:
                await self.readable()
                result: ServiceResult = await Coordination.run_worker(self.connection.recv)
                await result.received(self, observed)
        except (EOFError, ConnectionResetError):
            await Coordination.run_worker(partial(self.peer.await_end, 2))
            failed(self.ended(ConnectionClosed()))
        except ObserverDied as error:
            failed(error)

    @cached_property
    def closing(self) -> asyncio.Task[None]:
        return asyncio.create_task(self.retire())

    async def retire(self) -> None:
        self.status.retire(self)
        async with AsyncExitStack() as release:
            release.push_async_callback(Coordination.run_worker, self._resources.close)
            await self._reader.aclose()

    async def close(self) -> None:
        await join_retirement(self.closing)
