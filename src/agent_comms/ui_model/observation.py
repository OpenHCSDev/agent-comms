"""Core observation for the comms UI, in a process of its own.

A UI process asks what to observe (the sidebar, the threads its views show)
and receives finished results only when Core's stores actually change: the
service watches the store files that ``HistoryViews.revision_paths`` declares,
reads snapshots and presentations, derives the sidebar rows, and sends them
over a pipe. The UI also asks it for reads it awaits: store reads (transcript
pages, their currency, message notifications), answered in arrival order, and
reads a thread's live owner answers over its socket (the goal snapshot, input
delivery), answered as they complete. Each answer carries the request id and
the decoded value or the exception the read raised. The UI thread no longer
polls revisions, reads stores, resolves owners, decodes replies or derives
rows, and no longer competes with that work for its interpreter lock.
"""

from __future__ import annotations

import asyncio
import itertools
import multiprocessing
import signal
import sys
import time
import traceback
from abc import ABC, abstractmethod
from collections.abc import Callable, Generator, Iterator, Mapping
from contextlib import AbstractContextManager, ExitStack, aclosing, suppress
from dataclasses import dataclass, field, replace
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING, ClassVar, Self, final

from agent_comms.acp_extension import (
    GoalSnapshotReply,
    TranscriptDraftUpdate,
    TranscriptSnapshotUpdate,
)
from agent_comms.active_route import CommsRoute
from agent_comms.activity import ObservedActivity
from agent_comms.commander_hud import CommanderFrame, CommanderHud
from agent_comms.comms import Comms
from agent_comms.coordination_errors import (
    CoordinationError,
    CoordinationReadUnavailable,
    DeferredRead,
    ReloadRequired,
    Refused,
    StaleRevision,
)
from agent_comms.errors import RelationViolationError, ToolRefusedError
from agent_comms.field_codec import FieldCodec
from agent_comms.historical_views import HistorySource
from agent_comms.notification_evidence import NoNotificationWindow, NotificationWindow
from agent_comms.presentation import NoRevision, ReadState, WireRevision
from agent_comms.registry_document import RosterVisibility
from agent_comms.registry_provenance import DeletedThread
from agent_comms.revision_views import (
    CommanderFramesView,
    RegistrationsView,
    SidebarView,
    StoresView,
)
from agent_comms.ui_model.delivery import ReadDeliveryView
from agent_comms.ui_model.sidebar import (
    ChannelRowModel,
    SidebarModel,
    ThreadRowModel,
    WaitingSidebar,
)
from agent_comms.ui_model.status import OpenThreadStatus, ThreadStatusModel
from agent_comms.view_unread import ReplyIndexResult
from agent_comms.watched import FileSource

if TYPE_CHECKING:
    from concurrent.futures import Future
    from multiprocessing.connection import Connection

    from agent_comms.acp_extension import GoalSnapshotReply
    from agent_comms.comms import Comms
    from agent_comms.history_views import RetiredViews
    from agent_comms.message_reference import MessageReference
    from agent_comms.presentation import CoordinationSnapshot, MessageNotification
    from agent_comms.runtime import RuntimeConnection
    from agent_comms.thread_identity import ThreadIncarnation
    from agent_comms.thread_presentation import ThreadPresentation
    from agent_comms.transcripts import TranscriptCursor, TranscriptRead, TranscriptReadIdentity


class ObservationRequest(ABC):
    """A UI request; each declares how the service receives it."""

    @abstractmethod
    def receive(self, service: ObservationService) -> None: ...


class InterestRequest(ObservationRequest):
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
            observation.sent = NoSidebarSent()
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
            if revision.changed_since_any(self.read_revision):
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
class UnreadableHistory(ValueError):  # noqa: N818 - nominal stored-read state
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
class AwaitedRead(ObservationRequest):
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


class ReadRequest(AwaitedRead):
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
            return ReadAnswered(self.request_id, value)
        except self.refusal_errors as error:
            return self.refused(error)


@dataclass(frozen=True)
class OwnerRead(AwaitedRead):
    """A read the live owner of thread incarnation ``owner`` answers over its socket.

    Served as it arrives, beside store reads; the owner's reply is decoded
    here. Expected owner loss and refusals reach the caller with their original
    error types, without ending observation or inventing a successful snapshot.
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
            return ReadAnswered(self.request_id, await self.ask(comms))
        except self.refusal_errors as error:
            return self.refused(error)

    def connect(self, comms: Comms) -> RuntimeConnection:
        from agent_comms.runtime import OwnerIdentityChangedError, OwnerReplyConnection, socket_path

        owner = comms.registry.require(self.owner.name)
        if owner.incarnation != self.owner:
            raise OwnerIdentityChangedError("The owner incarnation changed while preparing the request.")
        return OwnerReplyConnection(
            comms, owner.name, socket_path(comms.root, owner.require_process().pid),
        )

    async def ask(self, comms: Comms) -> object:
        connection = await asyncio.to_thread(self.connect, comms)
        try:
            return await self.read(connection)
        finally:
            await connection.close()

    @abstractmethod
    async def read(self, owner: RuntimeConnection) -> object: ...


@dataclass(frozen=True)
class CancelRead(ObservationRequest):
    """The UI stopped waiting; a read not yet answered is dropped."""

    request_id: int

    def receive(self, service: ObservationService) -> None:
        service.pending.pop(self.request_id, None)
        if (asking := service.owner_reads.pop(self.request_id, None)) is not None:
            asking.cancel()


@dataclass(frozen=True)
class TranscriptRequest[ResultT](ReadRequest):
    """Capture one bounded read; its owner keeps the page and witness together."""

    thread: str
    before: TranscriptCursor | None = None
    after: TranscriptCursor | None = None
    through: TranscriptCursor | None = None
    historical_source: str | None = None

    def read(self, comms: Comms) -> ResultT:
        return UnreadableHistory.outcome(lambda: self.result(self.page_read(comms))).raised()

    def page_read(self, comms: Comms) -> TranscriptRead:
        return comms.transcripts.capture_page_read(
            self.thread,
            before=self.before,
            after=self.after,
            through=self.through,
            historical_source=self.historical_source,
        )

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


class TranscriptPublicationState(ABC):
    @abstractmethod
    def result(self, read: TranscriptRead) -> TranscriptSnapshotUpdate | TranscriptDraftUpdate: ...


class NotPublished(TranscriptPublicationState):
    def result(self, read: TranscriptRead) -> TranscriptSnapshotUpdate:
        return TranscriptSnapshotUpdate.from_read(read)


@dataclass(frozen=True)
class Published(TranscriptPublicationState):
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
            before=identity.before,
            after=identity.after,
            through=identity.through,
        )
        if read.content_current():
            return None
        return ReadTranscript(
            self.root, identity.requested_name, before=identity.before, after=identity.after,
            through=identity.through, historical_source=identity.historical_source,
        ).read(comms)


@dataclass(frozen=True)
class ReadNotifications(ReadRequest):
    """Recipient handling of the referenced original messages."""

    references: tuple[MessageReference, ...]

    def read(self, comms: Comms) -> NotificationWindow:
        return comms.views.message_notifications_for_references(self.references)

    def changed_since(self, comms: Comms, revision: WireRevision,
                      previous: NotificationsRead) -> bool:
        return previous.live_changed(comms, revision)


@dataclass(frozen=True)
class ReadRecordedNotifications(ReadNotifications):
    """The selected archive's recorded handling, independent of today's recipients."""

    source: str

    def read(self, comms: Comms) -> NotificationWindow:
        with comms.bus.history.read_sources() as sources:
            source = HistorySource.require(sources, self.source)
            return source.notification_references(sources, self.references)

    def changed_since(self, comms: Comms, revision: WireRevision,
                      previous: NotificationsRead) -> bool:
        return revision.history != previous.revision.history


@dataclass(frozen=True)
class NotificationsRead:
    """Where one notification read settled: its revision and the activity of every participant
    its notifications name, captured before the read for participants already known and right
    after it for new ones."""

    revision: WireRevision
    activity: Mapping[str, ObservedActivity | DeletedThread | None] = field(default_factory=dict)
    notification_window: NotificationWindow = field(default_factory=NoNotificationWindow)

    @classmethod
    def of(cls, revision: WireRevision, comms: Comms,
           known: Mapping[str, ObservedActivity | DeletedThread],
           window: NotificationWindow,
           ) -> NotificationsRead:
        names = window.activity_names()
        known = {**comms.views.observed_activity(revision, names - known.keys()), **known}
        return cls(revision, {name: known.get(name) for name in names}, window)

    def live_changed(self, comms: Comms, revision: WireRevision) -> bool:
        """Read marks, participant activity or retained assignment evidence changed."""
        if revision.registry != self.revision.registry:
            return True
        current = comms.views.observed_activity(revision, self.activity) if self.activity else {}
        return (any(current.get(name) != seen for name, seen in self.activity.items())
                or self.notification_window.changed(comms, revision, self.revision))


class UnreadableNotificationsRead(NotificationsRead):
    """An unreadable read names no participants, so any later revision reads again."""

    def live_changed(self, comms: Comms, revision: WireRevision) -> bool:
        return revision != self.revision


@dataclass(frozen=True)
class ReadOwnerGoal(OwnerRead):
    """The owner's goal snapshot and the turn it read with it."""

    async def read(self, owner: RuntimeConnection) -> GoalSnapshotReply:
        return FieldCodec.decode(GoalSnapshotReply, await owner.request("goal_snapshot"))


@dataclass(frozen=True)
class ReadDeliveryOverview(OwnerRead):
    """The owner's delivery notices, checked against its live queue."""

    include_history: bool = False

    async def read(self, owner: RuntimeConnection) -> ReadDeliveryView:
        return ReadDeliveryView.from_wire(
            await owner.request("input_dispositions", include_history=self.include_history)
        )


@dataclass(frozen=True)
class ObserveSidebar(InterestRequest):
    worktree: str
    show_stopped: bool
    show_archived: bool

    def observation(self) -> SidebarReading:
        return SidebarReading(self)

    def _answer(
        self, revision: WireRevision, read: Callable[..., CoordinationSnapshot], subject: str
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
        if snapshot.has_indexing_unread():
            reading.sent = NoSidebarSent()


class SidebarReading(ReadingObservation[ObserveSidebar]):
    def __init__(self, request: ObserveSidebar):
        super().__init__(request)
        self.stage: SidebarReadStage = RosterSidebarRead()
        self.sent: SendState = NoSidebarSent()
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
        key = SidebarSent(revision.expiry_period, type(self.stage), revision)
        if not self.sent.changed_since(key):
            return
        yield from self.stage.read(self.request, comms, revision).finish_read(self, key)

    def publish(self, result: SidebarResult, key: SidebarSent) -> Iterator[SidebarResult]:
        self.sent = result.send_state(key)
        result.advance(self)
        if result.accepted_by(self.published):
            self.published = result
            yield result


class SendState:
    def changed_since(self, current: SidebarSent) -> bool:
        return True


class NoSidebarSent(SendState):
    pass


@dataclass(frozen=True)
class SidebarSent(SendState):
    expiry_period: int
    stage: type[SidebarReadStage]
    revision: WireRevision = field(compare=False)

    def changed_since(self, current: SidebarSent) -> bool:
        return self.revision.changed_since(SidebarView(), current.revision) or self != current


@dataclass(frozen=True)
class StopSidebar(ObservationRequest):
    def receive(self, service: ObservationService) -> None:
        service.interest.remove(SidebarReading)


@dataclass(frozen=True)
class StopObserving(ObservationRequest):
    """Views are retiring; finish their outstanding reads without observing more state."""

    def receive(self, service: ObservationService) -> None:
        service.interest = service.interest.stop()


@dataclass(frozen=True)
class ObserveThreads(InterestRequest):
    names: frozenset[str]

    def observation(self) -> ThreadReading:
        return ThreadReading(self)


@dataclass(frozen=True)
class ObserveCommanderHud(InterestRequest):
    """Each commander's newest heartbeat frame; Core derived it, a view only shows it."""

    def observation(self) -> CommanderHudReading:
        return CommanderHudReading(self)


@dataclass(frozen=True)
class StopCommanderHud(ObservationRequest):
    def receive(self, service: ObservationService) -> None:
        service.interest.remove(CommanderHudReading)


class CommanderHudReading(ReadingObservation[ObserveCommanderHud]):
    """Reads the frame files only when the heartbeat replaced one of them."""

    def __init__(self, request: ObserveCommanderHud):
        super().__init__(request)
        self.revision: ReadState = NoRevision()

    def read(self, comms: Comms, revision: WireRevision) -> Generator[ObservationStep]:
        if not self.revision.changed_since(CommanderFramesView(), revision):
            return
        self.revision = revision
        yield CommanderHudObserved(revision, CommanderFrame.read_all(comms.root))


@dataclass(frozen=True)
class ObserveViews(InterestRequest):
    """The thread incarnations and channel views open in the UI, to learn when one is retired."""

    threads: frozenset
    channels: frozenset[str]

    def observation(self) -> ViewReading:
        return ViewReading(self)


@dataclass(frozen=True)
class ObserveNotifications(InterestRequest):
    """Original message references currently visible in the open views."""

    reads: frozenset[ReadNotifications]

    def observation(self) -> NotificationReading:
        return NotificationReading(self)


class NotificationReading(ReadingObservation[ObserveNotifications]):
    def __init__(self, request: ObserveNotifications):
        super().__init__(request)
        self.read_states: dict[ReadNotifications, NotificationsRead] = {}

    def read(self, comms: Comms, revision: WireRevision) -> Generator[ObservationStep]:
        for request in self.request.reads:
            previous = self.read_states.get(request)
            if previous is not None and not request.changed_since(comms, revision, previous):
                continue
            yield self.notifications(request, comms, revision, previous)

    def notifications(self, request: ReadNotifications, comms: Comms, revision: WireRevision,
                      previous: NotificationsRead | None) -> ObservationStep:
        known = comms.views.observed_activity(revision, () if previous is None else previous.activity)

        def read() -> NotificationWindow:
            request.require_root(comms)
            return request.read(comms)

        def settled(result: Observed, state: NotificationsRead) -> Observed:
            self.read_states[request] = state
            return result
        return UnreadableHistory.outcome(read).answer(
            lambda window: settled(NotificationsObserved(revision, request, window.outcomes),
                                   NotificationsRead.of(revision, comms, known, window)),
            lambda error: ObservationPending(),  # Keep the interest until its source can be read.
            lambda error: settled(NotificationsUnreadable(revision, request, error),
                                  UnreadableNotificationsRead(revision)))


@dataclass(frozen=True)
class LocatedThreads:
    activity: Mapping[str, ObservedActivity | DeletedThread]
    names: frozenset[str]
    reads: Mapping[str, Callable[[], ThreadPresentation]]


class ThreadReading(ReadingObservation[ObserveThreads]):
    def __init__(self, request: ObserveThreads):
        super().__init__(request)
        self.observed = ThreadsObserved(WireRevision.empty(), {}, frozenset(), {})
        self.initial = request.names
        self.located_threads = LocatedThreads({}, request.names, {})

    def continue_from(self, previous: Self) -> Self:
        self.observed = previous.observed
        self.initial = self.request.names - previous.request.names
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
                       if name in self.request.names and name not in located.names)
        observed = ThreadsObserved(revision, results, frozenset(), located.activity)
        self.observed = observed
        changed = observed.changed_since(previous) | self.initial
        self.initial = frozenset()
        if changed:
            yield replace(observed, changed=changed,
                          results={name: value for name, value in results.items() if name in changed})

    def located(self, comms: Comms, revision: WireRevision, previous: ThreadsObserved) -> LocatedThreads:
        # Keep each completed stage when a later stored read fails, exactly as
        # the original read did before its activity/names became declared state.
        self.located_threads = LocatedThreads({}, self.request.names, {})
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


class ServiceResult(ABC):
    """What the service sends; each declares how the UI side receives it."""

    def retire_request(self, service: ObservationService, request: ReadRequest) -> None:
        service.pending.pop(request.request_id)

    @abstractmethod
    def received(self, process: ObservationProcess, observed: list[Observed]) -> None: ...


class ReadOutcome(ServiceResult):
    """The service's answer to a read, including a read that remains busy."""


@dataclass(frozen=True)
class Observed(ObservationStep, ServiceResult):
    """A result, as of one revision of Core's stores."""

    revision: WireRevision

    def publish(self, service: ObservationService) -> None:
        service.send(self)

    def received(self, process: ObservationProcess, observed: list[Observed]) -> None:
        observed.append(self)


@dataclass(frozen=True)
class ReadAnswered(ReadOutcome):
    request_id: int
    value: object

    def received(self, process: ObservationProcess, observed: list[Observed]) -> None:
        if (waiter := process.reads.pop(self.request_id, None)) is not None and not waiter.done():
            waiter.set_result(self.value)


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

    def received(self, process: ObservationProcess, observed: list[Observed]) -> None:
        # Keep the same waiter. The service owns waiting for the source and
        # answers this request when its next observation can acquire a read.
        pass


@dataclass(frozen=True)
class ReadRefused(ReadOutcome):
    """The read raised ``error``; the UI caller receives that same exception."""

    request_id: int
    error: Exception
    trace: str

    def received(self, process: ObservationProcess, observed: list[Observed]) -> None:
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
class SidebarResult(PublishState, Observed):
    revision: WireRevision = field(compare=False)
    request: ObserveSidebar

    def finish_read(self, reading: SidebarReading, key: SidebarSent) -> Iterator[SidebarResult]:
        return reading.publish(self, key)

    def advance(self, reading: SidebarReading) -> None:
        """Keep this stage until its source revision changes."""

    def send_state(self, key: SidebarSent) -> SendState:
        return key

    def accepted_by(self, previous: PublishState) -> bool:
        return not previous.same_observation(self)


@dataclass(frozen=True)
class SidebarPreparing(SidebarResult, WaitingSidebar):
    """The first answer explains the unfinished read without replacing real rows."""

    cause: DeferredRead

    def send_state(self, key: SidebarSent) -> SendState:
        return NoSidebarSent()

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


class ThreadReadResult(PublishState, ABC):
    """One thread's last read, including how to compare and display its answer."""

    def same_as_presented(self, presented: ThreadPresented) -> bool:
        return False

    @abstractmethod
    def status_row(self, name: str, owner: OpenThreadStatus) -> ThreadStatusModel: ...

    def collect_revision_inputs(self, name: str, readable: dict[str, ThreadPresentation], retry: set[str]) -> None:
        """Supply readable values and reads that must be tried again."""

    def same_status(self, presentation: ThreadPresentation) -> bool:
        return False


@dataclass(frozen=True)
class ThreadPresented(ThreadReadResult):
    presentation: ThreadPresentation

    def same_observation(self, previous: ThreadReadResult) -> bool:
        return previous.same_as_presented(self)

    def same_as_presented(self, presented: ThreadPresented) -> bool:
        return (presented.presentation is self.presentation
                or presented.presentation.same_observation(self.presentation))

    def status_row(self, name: str, owner: OpenThreadStatus) -> ThreadStatusModel:
        return ThreadStatusModel.known(name, self.presentation)

    def collect_revision_inputs(self, name: str, readable: dict[str, ThreadPresentation], retry: set[str]) -> None:
        self.presentation.collect_revision_input(name, readable)

    def same_status(self, presentation: ThreadPresentation) -> bool:
        return presentation.same_status(self.presentation)


@dataclass(frozen=True)
class ThreadUnavailable(ThreadReadResult):
    error: UnreadableHistory

    def status_row(self, name: str, owner: OpenThreadStatus) -> ThreadStatusModel:
        return ThreadStatusModel.unreadable(name, self.error)


@dataclass(frozen=True)
class ThreadBusy(ThreadReadResult):
    def status_row(self, name: str, owner: OpenThreadStatus) -> ThreadStatusModel:
        return owner.open(name)

    def collect_revision_inputs(self, name: str, readable: dict[str, ThreadPresentation], retry: set[str]) -> None:
        retry.add(name)


@dataclass(frozen=True)
class ThreadsObserved(Observed):
    """One mutually exclusive read answer per observed thread."""

    results: Mapping[str, ThreadReadResult]
    changed: frozenset[str]
    activity: Mapping[str, ObservedActivity | DeletedThread]

    def revision_inputs(self) -> tuple[dict[str, ThreadPresentation], set[str]]:
        readable: dict[str, ThreadPresentation] = {}
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
class NotificationsObserved(Observed):
    request: ReadNotifications
    outcomes: Mapping[tuple[int, str], tuple[MessageNotification, ...]]


@dataclass(frozen=True)
class NotificationsUnreadable(Observed):
    request: ReadNotifications
    error: UnreadableHistory


@dataclass(frozen=True)
class CommanderHudObserved(Observed):
    """Every beating commander's newest frame, by commander name."""

    huds: tuple[CommanderHud, ...]


@dataclass(frozen=True)
class ViewsRetired(Observed):
    """Open views whose thread was deleted or replaced, or whose channel is gone or archived."""

    retired: RetiredViews


@dataclass(frozen=True)
class ObservationFailed(ServiceResult):
    """The service failed and is exiting; ``error`` is its traceback."""

    error: str

    def received(self, process: ObservationProcess, observed: list[Observed]) -> None:
        raise process.ended(self.error)


class ObserverStatus(ABC):
    """The reported observer outcome owns whether a request can proceed."""

    @abstractmethod
    def raise_if_failed(self) -> None: ...

    def failure(self, create: Callable[[], ObserverDied]) -> ObserverDied:
        return create()


class NoObserverFailure(ObserverStatus):
    def raise_if_failed(self) -> None:
        return None


class ObserverDied(CoordinationError, ObserverStatus):
    """The observation service's process ended while the UI was using it.

    The UI does not run on what it last observed: it reports this and exits.
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


class ObservationProcess(AbstractContextManager):
    """Own reader retirement, awaited reads, the pipe and the joined service, in that order."""

    @classmethod
    def awaited(cls, route: CommsRoute) -> Self:
        """Attach readiness to the process lifetime, not a suspended generator's finalizer."""
        loop = asyncio.get_running_loop()
        process = cls(route)
        descriptor = process.fileno()
        try:
            loop.add_reader(descriptor, process.answer_reads)
        except BaseException:
            process.close()
            raise
        process._reader.callback(loop.remove_reader, descriptor)
        return process

    def __init__(self, route: CommsRoute):
        self._resources = ExitStack()
        self.request_ids = itertools.count(1)
        self.reads: dict[int, asyncio.Future] = {}
        self.status: ObserverStatus = NoObserverFailure()
        context = multiprocessing.get_context("spawn")
        with ExitStack() as acquisition:
            self.connection, child = context.Pipe()
            acquisition.enter_context(self.connection)
            with child:
                self.process = context.Process(target=serve, args=(route, child), daemon=False,
                                               name="agent-comms-observation")
                self.process.start()
            self._resources.callback(self.process.join)
            self._resources.enter_context(acquisition.pop_all())
        self._resources.callback(
            self.abandon_reads,
            StaleRevision("The Comms observation retired before the read was answered"),
        )
        self._reader = self._resources.enter_context(ExitStack())

    def __exit__(self, *exc) -> None:
        self.close()

    def fileno(self) -> int:
        return self.connection.fileno()

    def request(self, request: ObservationRequest) -> None:
        """Tell the service what the UI shows, or report its end to this caller."""
        self.status.raise_if_failed()
        if self.connection.closed:
            raise StaleRevision("Cannot request a retired Comms observation process")
        try:
            self.connection.send(request)
        except BrokenPipeError as error:
            died = self.ended(str(error))
        else:
            return
        # The pickler's failed-send traceback retains an exported BytesIO
        # view. ObserverDied carries the cause text, not that pipe buffer.
        raise died

    def ended(self, error: str | None) -> ObserverDied:
        """The service ended (``error``: the traceback it sent): fail its awaited reads and say how it exited."""
        self._reader.close()
        self.process.join(timeout=2)
        died = self.status.failure(
            partial(ObserverDied, self.process.pid, self.process.exitcode, error)
        )
        self.status = died
        self.abandon_reads(died)
        return died

    async def read(self, request: AwaitedRead):
        """Send ``request`` and await its answer, delivered by ``results``.

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

    def abandon_reads(self, error: Exception) -> None:
        """The reader's retirement settles its waiters with the original cause."""
        reads, self.reads = self.reads, {}
        for waiter in reads.values():
            if not waiter.done():
                waiter.set_exception(error)

    def results(self) -> list[Observed]:
        """Every observation waiting on the pipe; answered reads resolve their waiters.

        The service's end (its failure, or its pipe closing) is raised here
        as ``ObserverDied``; its owned readiness reader is retired then.
        """
        observed: list[Observed] = []
        while not self.connection.closed and self.connection.poll():
            try:
                result = self.connection.recv()
            except (EOFError, ConnectionResetError):
                raise self.ended(None) from None
            result.received(self, observed)
        return observed

    def answer_reads(self) -> None:
        """The loop's reader for awaited reads: answered reads resolve their waiters.

        The service's end has no caller here. ``ended`` already settles it into
        every awaited read and into the status the next request raises.
        """
        with suppress(ObserverDied):
            self.results()

    def close(self) -> None:
        self._resources.close()
