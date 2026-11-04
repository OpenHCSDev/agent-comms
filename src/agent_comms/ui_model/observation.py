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
from agent_comms.repo import Checkout

import asyncio
import itertools
import multiprocessing
import os
import socket
import struct
import signal
import sys
import time
import traceback
from abc import ABC, abstractmethod
from collections.abc import Callable, Generator, Iterator, Mapping
from contextlib import AbstractAsyncContextManager, AsyncExitStack, ExitStack, aclosing
from dataclasses import dataclass, field, replace
from functools import cached_property, partial
from pathlib import Path
from typing import TYPE_CHECKING, ClassVar, Self, final


from agent_comms_hooks import ToolRefusedError
from metaclass_registry.mro_dispatch import MroDispatch, handles

from agent_comms.acp_extension import (
    GoalSnapshotReply,
    TranscriptDraftUpdate,
    TranscriptSnapshotUpdate,
)
from agent_comms.active_route import CommsRoute
from agent_comms.activity import ObservedActivity
from agent_comms.bus_read import OwnerScopedRead, UiRead
from agent_comms.child_process import join_retirement
from agent_comms.input_disposition import DeliveryOverview
from agent_comms.team_hud import TeamFrame, TeamHud, TeammateDetail
from agent_comms.comms import Comms
from agent_comms.coordinator import Coordination
from agent_comms.coordination_errors import (
    CoordinationError,
    CoordinationReadUnavailable,
    DeferredRead,
    ReloadRequired,
    Refused,
    StaleRevision,
)
from agent_comms.errors import RelationViolationError
from agent_comms.history_views import RetiredViews
from agent_comms.message_notification import NotificationPass, NotificationWindow
from agent_comms.notification_read import NotificationsRead, UnreadableNotificationsRead
from agent_comms.message_reference import MessageReference
from agent_comms.nominal.field_codec import TaggedUnion
from agent_comms.nominal.field_codec import DeclaredError, FieldCodec, MessageId, TaggedRecordMember
from agent_comms.nominal.fold import Absorbs, Accumulates, Fold
from agent_comms.notification_reads import NotificationRead
from agent_comms.read_basis import DisplayCut, NoDisplaySelection
from agent_comms.message_notification import MessageNotification
from agent_comms.presentation import CoordinationSnapshot, NoRevision, ReadState, WireRevision
from agent_comms.registry_document import RosterVisibility
from agent_comms.registry_provenance import DeletedThread
from agent_comms.revision_views import (
    TeamFramesView,
    NotificationsView,
    RegistrationsView,
    RevisionView,
    SidebarView,
    StatusView,
    StoresView,
)
from agent_comms.thread_identity import ThreadIncarnation
from agent_comms.transcripts import AcquiredThreadPresentation
from agent_comms.thread_presentation import ThreadPresentation
from agent_comms.order_tools import TranscriptPageRead
from agent_comms.transcripts import TranscriptReadIdentity
from agent_comms.ui_model.sidebar import (
    ChannelRowModel,
    SidebarModel,
    ThreadRowModel,
    WaitingSidebar,
)
from agent_comms.ui_model.status import OpenThreadStatus, ThreadStatusModel
from agent_comms.view_unread import ReplyIndexResult
from agent_comms.watched import FileSource
from agent_comms.work_branches import WorkBranchError

from multiprocessing.connection import Connection

if TYPE_CHECKING:
    from aiohttp import ClientWebSocketResponse, web

    type WebSocketFrames = ClientWebSocketResponse | web.WebSocketResponse
    from concurrent.futures import Future

    from agent_comms.tool_result import ToolResult
    from agent_comms.transcripts import TranscriptRead


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

    def prepared(self, observation: SidebarReading, service: ObservationService) -> None:
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

    def prepared(self, observation: SidebarReading, service: ObservationService) -> None:
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
        """Run a stored read: its value, a read deferred to retry, or unreadable with its cause."""
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
    """A stored read's outcome: its value, or one of the two ways it fails."""

    @abstractmethod
    def answer[R](self, value: Callable[[T], R], deferred: Callable[[DeferredRead], R],
                  unreadable: Callable[[UnreadableHistory], R]) -> R: ...

    @abstractmethod
    def raised(self) -> T:
        """The value, or the failure raised again for a caller that answers it itself."""


@dataclass(frozen=True)
class ReadValue[T](StoredRead[T]):
    value: T

    def answer[R](self, value: Callable[[T], R], deferred: Callable[[DeferredRead], R],
                  unreadable: Callable[[UnreadableHistory], R]) -> R:
        return value(self.value)

    def raised(self) -> T:
        return self.value


@dataclass(frozen=True)
class ReadDeferred[T](StoredRead[T]):
    """Currentness or contention: the existing read is retried, not shown as broken."""

    error: DeferredRead

    def answer[R](self, value: Callable[[T], R], deferred: Callable[[DeferredRead], R],
                  unreadable: Callable[[UnreadableHistory], R]) -> R:
        return deferred(self.error)

    def raised(self) -> T:
        raise self.error


@dataclass(frozen=True)
class ReadUnreadable[T](StoredRead[T]):
    error: ValueError
    cause: UnreadableHistory

    def answer[R](self, value: Callable[[T], R], deferred: Callable[[DeferredRead], R],
                  unreadable: Callable[[UnreadableHistory], R]) -> R:
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
        return (DeferredRead, RelationViolationError, UnreadableHistory)

    def require_root(self, comms: Comms) -> None:
        if Path(self.root).expanduser().resolve() != comms.root.resolve():
            raise StaleRevision("The read belongs to another Comms root than the observed one")

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
            except ReloadRequired:
                raise
            except self.deferred_reads:
                return ReadBusy(self.request_id)
            return ReadAnswered.of(self.request_id, value)
        except self.refusal_errors as error:
            return self.refused(error)


@dataclass(frozen=True)
class ReadOperation(ReadRequest):
    """The UI's root and request witness around one root-free shared read."""

    operation: UiRead

    @property
    def refusal_errors(self) -> tuple[type[Exception], ...]:
        return (*super().refusal_errors, ToolRefusedError, WorkBranchError)

    def read(self, comms: Comms) -> object:
        return self.operation.read(comms)


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

    def page_read(self, comms: Comms) -> TranscriptRead:
        return self.operation.capture_as(comms, NoDisplaySelection())

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
            return TranscriptDraftUpdate(read.identity, read.current_draft().external_row())
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
        return UnreadableHistory.outcome(lambda: operation.read(comms)).raised()


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

    operation: OwnerScopedRead

    async def ask(self, comms: Comms) -> ToolResult:
        cut = await Coordination.run_worker(comms.registry.store.snapshot_with_authority)
        return await self.operation.answer(comms, self.owner, cut)


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
            lambda error: SidebarUnreadable(revision, self, error))


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
    """Each teammate's newest heartbeat frame; Core derived it, a view only shows it."""

    def observation(self) -> TeamHudReading:
        return TeamHudReading(self)


@dataclass(frozen=True)
class StopTeamHud(TaggedRecordMember, ObservationRequest):
    def receive(self, service: ObservationService) -> None:
        service.interest.remove(TeamHudReading)


class TeamHudReading(ReadingObservation[ObserveTeamHud]):
    """Reads the frame files only when the heartbeat replaced one of them."""

    view = TeamFramesView()

    def read(self, comms: Comms, revision: WireRevision) -> Generator[ObservationStep]:
        yield UnreadableHistory.outcome(
            lambda: TeamHudObserved(revision, TeamFrame.read_all(comms.root)),
        ).answer(
            lambda observed: observed,
            lambda error: ObservationPending(),  # Keep the interest until its source can be read.
            lambda error: TeamHudUnreadable(revision, error),
        )


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
                NotificationsUnreadable(revision, request, error)))

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

    def continue_from(self, previous: Self) -> Self:
        self.observed = previous.observed
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
            lambda error: self.failed(located.names, ThreadUnavailable(error), results))
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
        names = (revision.changed_threads(previous, comms, activity) | names - previous.results.keys()) & names
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
                lambda error: ThreadBusy(), ThreadUnavailable)
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


class ReadOutcome(TaggedRecordMember, ServiceResult, declared_name=None):
    """The service's answer to a read, including a read that remains busy."""


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

    answers: ClassVar[dict[type, type[ReadAnswered]]] = {}

    request_id: int
    value: object

    def __init_subclass__(cls, *, answers: type, **kwargs: object) -> None:
        super().__init_subclass__(**kwargs)
        ReadAnswered.answers[answers] = cls

    @staticmethod
    def of(request_id: int, value: object) -> ReadAnswered:
        """The boundary: a read's result reaches the wire through its declared answer member."""
        for kind in type(value).__mro__:
            if kind in ReadAnswered.answers:
                return ReadAnswered.answers[kind](request_id, value)
        raise TypeError(f"No read answer is declared for {type(value).__qualname__}")

    async def received(self, process: ObservationProcess, observed: Callable[[Observed], None]) -> None:
        if (waiter := process.reads.pop(self.request_id, None)) is not None and not waiter.done():
            waiter.set_result(self.value)


@dataclass(frozen=True)
class NothingAnswered(ReadAnswered, answers=type(None)):
    value: None = None


@dataclass(frozen=True)
class TranscriptAnswered(ReadAnswered, answers=TranscriptSnapshotUpdate):
    value: TranscriptSnapshotUpdate


@dataclass(frozen=True)
class DraftAnswered(ReadAnswered, answers=TranscriptDraftUpdate):
    value: TranscriptDraftUpdate


@dataclass(frozen=True)
class NotificationsAnswered(ReadAnswered, answers=NotificationWindow):
    value: NotificationWindow


@dataclass(frozen=True)
class TeammateAnswered(ReadAnswered, answers=TeammateDetail):
    value: TeammateDetail


@dataclass(frozen=True)
class GoalAnswered(ReadAnswered, answers=GoalSnapshotReply):
    value: GoalSnapshotReply


@dataclass(frozen=True)
class DeliveryAnswered(ReadAnswered, answers=DeliveryOverview):
    value: DeliveryOverview


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


@dataclass(frozen=True)
class ReadRefused(ReadOutcome):
    """The read raised ``error``; the UI caller receives that same exception."""

    request_id: int
    error: Exception
    trace: str

    async def received(self, process: ObservationProcess, observed: Callable[[Observed], None]) -> None:
        if (waiter := process.reads.pop(self.request_id, None)) is not None and not waiter.done():
            self.error.add_note(f"Raised in the comms observation service:\n{self.trace}")
            waiter.set_exception(self.error)


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

    cause: DeferredRead

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
        await Coordination.run_worker(partial(process.peer.join, 2))
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


class ObserverRetired(StaleRevision, ObserverStatus):
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


class ObserverDied(CoordinationError, ObserverStatus):
    """The observation service ended; reads share its reported cause.

    Presentation and recovery belong to the consuming application.
    """

    def __init__(self, pid: int, exit_status: int | None, error: str | None):
        self.pid, self.exit_status, self.error = pid, exit_status, error
        if exit_status is None:
            how = "closed its pipe and has not exited"
        elif exit_status < 0:
            how = f"was killed by signal {-exit_status} ({signal.strsignal(-exit_status)})"
        else:
            how = f"exited with status {exit_status}"
        super().__init__(f"The comms observer (pid {pid}) {how}" + (f":\n{error}" if error else ""))

    def raise_if_failed(self) -> None:
        raise self

    def failure(self, create: Callable[[], ObserverDied]) -> ObserverDied:
        return self


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


async def relay_frames(connection: Connection, peer: WebSocketFrames) -> str:
    """Carry frames both ways between a local connection and a WebSocket until either side
    ends, then close the WebSocket, stop both directions and say which side ended first.

    The local connection stays open: its owner records the returned reason and then closes
    it, which also wakes a worker thread still blocked on it. Socket reads and writes run in
    worker threads, so a peer that stops reading never blocks this loop. A direction that
    fails in any other way raises its own error here."""
    from aiohttp import WSMsgType

    ended: asyncio.Future[str] = asyncio.get_running_loop().create_future()

    def end(reason: str) -> None:
        """The first side to end names the relay's end; a later one only follows it."""
        if not ended.done():
            ended.set_result(reason)

    async def outward() -> None:
        try:
            while True:
                await peer.send_bytes(await asyncio.to_thread(connection.recv_bytes))
        except (EOFError, OSError) as error:
            # The local socket's own end-of-stream: this is how its side ends.
            end(f"the local side ended ({type(error).__name__})")

    async def inward() -> None:
        try:
            async for message in peer:
                if message.type is not WSMsgType.BINARY:
                    end(f"the peer sent a {message.type.name} frame")
                    return
                await asyncio.to_thread(connection.send_bytes, message.data)
        except OSError as error:
            # The local socket's write deadline: the local side stopped reading.
            end(f"the local side stopped reading ({type(error).__name__})")
            return
        end("the peer closed its socket")

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
        return FieldCodec.decode_document(self.receives, FieldCodec.parse(self.recv_bytes().decode()))


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
    def join(self, timeout: float | None = None) -> None:
        """Wait for the service to end; never ends it."""

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

    def __init__(self, route: CommsRoute) -> None:
        context = multiprocessing.get_context("spawn")
        self.connection, child = context.Pipe()
        with ExitStack() as acquisition:
            acquisition.enter_context(self.connection)
            with child:
                self.process = context.Process(target=serve, args=(route, child), daemon=False,
                                               name="agent-comms-observation")
                self.process.start()
            acquisition.pop_all()

    def join(self, timeout: float | None = None) -> None:
        self.process.join(timeout)

    def service_failed(self, traceback: str) -> ObserverDied:
        return ObserverDied(self.process.pid, self.process.exitcode, traceback)

    def connection_closed(self) -> ObserverDied:
        return ObserverDied(self.process.pid, self.process.exitcode, None)

    def connection_broken(self, error: str) -> ObserverDied:
        return ObserverDied(self.process.pid, self.process.exitcode, error)


class ObservationProcess(AbstractAsyncContextManager, MroDispatch):
    """One ordered receive/decode task, loop-owned delivery, and joined retirement."""

    @classmethod
    def awaited(cls, route: CommsRoute) -> Self:
        process = cls(route)
        process.start_receiving(process.observed, process.failed)
        return process

    def __init__(self, source: CommsRoute | ObservationSource):
        """``source`` names who serves the observation: a route serves it locally, a remote
        UI profile across the connector."""
        self._resources = ExitStack()
        self.request_ids = itertools.count(1)
        self.reads: dict[int, asyncio.Future] = {}
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
        """Start this process's reader on the delivery loop, once at attachment."""
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

    def request(self, request: ObservationRequest) -> None:
        """Tell the service what the UI shows, or report its end to this caller."""
        self.status.raise_if_failed()
        if self.connection.closed:
            raise StaleRevision("Cannot request a retired Comms observation process")
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

    async def read(self, request: AwaitedRead):
        """Send ``request`` and await its loop-delivered answer.

        Cancelling the caller cancels the read; an unserved read is dropped.
        """
        request = replace(request, request_id=next(self.request_ids))
        waiter = asyncio.get_running_loop().create_future()
        self.reads[request.request_id] = waiter
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
        for waiter in reads.values():
            if not waiter.done():
                waiter.set_exception(error)

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
            await Coordination.run_worker(partial(self.peer.join, 2))
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
