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
import os
import signal
import sys
import time
import traceback
from abc import ABC, abstractmethod
from collections.abc import Callable, Iterable, Iterator, Mapping
from contextlib import AbstractContextManager, ExitStack
from dataclasses import dataclass, field, replace
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING, ClassVar, Self, final

from acp import RequestError

from agent_comms.acp_extension import TranscriptDraftUpdate, TranscriptSnapshotUpdate
from agent_comms.active_route import CommsRoute
from agent_comms.activity import ObservedActivity
from agent_comms.bus_route_counts import RouteIndexRebuilding
from agent_comms.coordination_errors import (
    CoordinationError,
    CoordinationReadUnavailable,
    StaleRevision,
)
from agent_comms.errors import RelationViolationError
from agent_comms.field_codec import FieldCodec
from agent_comms.presentation import NoRevision, ReadState, WireRevision
from agent_comms.registry_document import DEFAULT_ROSTER, RosterVisibility
from agent_comms.revision_views import NotificationsView, RegistrationsView, SidebarView, StoresView
from agent_comms.ui_model.delivery import InputDelivery
from agent_comms.ui_model.sidebar import ChannelRowModel, SidebarModel, ThreadRowModel
from agent_comms.ui_model.status import OpenThreadStatus, ThreadStatusModel
from agent_comms.view_unread import ReplyIndexResult

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
        self.observations.clear()
        return StoppedObservationInterest()

    def remove(self, kind: type[Observation]) -> None:
        self.observations.pop(kind, None)

    def prepared(self, observation: SidebarReading, service: ObservationService) -> None:
        if self.observations.get(type(observation)) is observation:
            observation.sent = NoSidebarSent()
            service.observe_again()

    async def observe(self, service: ObservationService, revision: WireRevision,
                      attempted: set[int]) -> None:
        for kind, observation in tuple(self.observations.items()):
            await service.answer_pending(attempted)
            if self.observations.get(kind) is not observation:
                continue
            for result in observation.read(service.comms, revision):
                result.publish(service)
                await service.answer_pending(attempted)
                if self.observations.get(kind) is not observation:
                    break
            if self.observations.get(kind) is observation:
                observation.after_read(service)


class ObservationLifetime:
    def for_interest(self, requested: Observation) -> Observation:
        return requested


class NotObserved(ObservationLifetime):
    pass


class ObservationStep:
    def publish(self, service: ObservationService) -> None:
        pass


class ObservationPending(ObservationStep):
    pass


class Observation[InterestT: InterestRequest](ObservationLifetime):
    """A requested view owns its read state and yields between store operations."""

    def __init__(self, request: InterestT):
        self.request = request

    def for_interest(self, requested: Observation) -> Observation:
        return self if self.request == requested.request else requested.continue_from(self)

    def continue_from(self, previous: Self) -> Self:
        """A changed interest starts fresh unless its reader can retain useful answers."""
        return self

    @abstractmethod
    def read(self, comms: Comms, revision: WireRevision) -> Iterator[ObservationStep]: ...

    def after_read(self, service: ObservationService) -> None:
        """Subscribe to completion when this observation has unfinished preparation."""


@final
class UnreadableHistory(ValueError):  # noqa: N818 - nominal stored-read state
    """A strict stored-data read failed, not the observer or the whole UI.

    The reader runtime is known; the writer version is not inferred from a
    missing declaration. Older saved records can fail after a declaration is
    deleted just as newer records can fail in an older reader.
    """

    # Currentness and contention defer the existing read; they do not show
    # broken saved data.
    DEFERRED_READS = (CoordinationReadUnavailable, StaleRevision)
    # Every way Core refuses saved data is a ValueError: StoredHistoryUnreadable and the other
    # relation violations, a codec's refusal of a malformed record, an archive's source checks,
    # and UnreadableHistory itself.
    UNREADABLE_READS = (ValueError,)

    @classmethod
    def outcome[T](cls, read: Callable[[], T]) -> StoredRead[T]:
        """Run a stored read: its value, a read deferred to retry, or unreadable with its cause."""
        try:
            return ReadValue(read())
        except RouteIndexRebuilding as error:
            return ReadPreparing(error)
        except cls.DEFERRED_READS as error:
            return ReadDeferred(error)
        except cls.UNREADABLE_READS as error:
            return ReadUnreadable(error, cls.capture(error))

    def __eq__(self, other: object) -> bool:
        return isinstance(other, UnreadableHistory) and self.args == other.args

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
    def answer[R](self, value: Callable[[T], R], deferred: Callable[[], R],
                  unreadable: Callable[[UnreadableHistory], R]) -> R: ...

    @abstractmethod
    def raised(self) -> T:
        """The value, or the failure raised again for a caller that answers it itself."""

    def continued(self) -> Self:
        """This outcome, for a reader that runs the next unit of a bounded preparation."""
        return self


@dataclass(frozen=True)
class ReadValue[T](StoredRead[T]):
    value: T

    def answer[R](self, value: Callable[[T], R], deferred: Callable[[], R],
                  unreadable: Callable[[UnreadableHistory], R]) -> R:
        return value(self.value)

    def raised(self) -> T:
        return self.value


@dataclass(frozen=True)
class ReadDeferred[T](StoredRead[T]):
    """Currentness or contention: the existing read is retried, not shown as broken."""

    error: CoordinationError

    def answer[R](self, value: Callable[[T], R], deferred: Callable[[], R],
                  unreadable: Callable[[UnreadableHistory], R]) -> R:
        return deferred()

    def raised(self) -> T:
        raise self.error


@dataclass(frozen=True)
class ReadPreparing[T](ReadDeferred[T]):
    """A bounded route index unit finished and runnable work remains."""

    def continued(self) -> Self:
        raise self.error


@dataclass(frozen=True)
class ReadUnreadable[T](StoredRead[T]):
    error: ValueError
    cause: UnreadableHistory

    def answer[R](self, value: Callable[[T], R], deferred: Callable[[], R],
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

    def answer(self, comms: Comms) -> ReadAnswered | ReadRefused | ReadBusy:
        try:
            self.require_root(comms)
            try:
                value = self.read(comms)
            except self.deferred_reads:
                return ReadBusy(self.request_id)
            return ReadAnswered(self.request_id, value)
        except (CoordinationReadUnavailable, RelationViolationError, UnreadableHistory, StaleRevision) as error:
            return self.refused(error)


@dataclass(frozen=True)
class OwnerRead(AwaitedRead):
    """A read the live owner of thread incarnation ``owner`` answers over its socket.

    Served as it arrives, beside store reads; the owner's reply is decoded
    here. The owner's refusals and a changed owner identity reach the UI as
    ValueError, as the UI's owner actions report them.
    """

    owner: ThreadIncarnation

    def receive(self, service: ObservationService) -> None:
        service.owner_reads[self.request_id] = service.tasks.create_task(self.serve(service))

    async def serve(self, service: ObservationService) -> None:
        try:
            answer = await self.answer(service.comms)
        finally:
            service.owner_reads.pop(self.request_id, None)
        service.send(answer)

    async def answer(self, comms: Comms) -> ReadAnswered | ReadRefused:
        try:
            self.require_root(comms)
            return ReadAnswered(self.request_id, await self.ask(comms))
        except (OSError, ValueError, RuntimeError, RequestError) as error:
            return self.refused(error)

    def connect(self, comms: Comms) -> RuntimeConnection:
        from agent_comms.runtime import OwnerReplyConnection, socket_path

        owner = comms.registry.require(self.owner.name)
        if owner.incarnation != self.owner:
            raise ValueError("The owner incarnation changed while preparing the request.")
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

    deferred_reads: ClassVar[tuple[type[Exception], ...]] = UnreadableHistory.DEFERRED_READS

    def result(self, read: TranscriptRead) -> TranscriptSnapshotUpdate:
        return TranscriptSnapshotUpdate.from_read(read)


class SubscribeTranscript(ReadTranscript):
    """A subscription answers contention instead of waiting for another revision."""

    deferred_reads: ClassVar[tuple[type[Exception], ...]] = tuple(
        error for error in ReadTranscript.deferred_reads
        if not issubclass(error, CoordinationReadUnavailable)
    )


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

    def read(self, comms: Comms) -> Mapping[tuple[int, str], tuple[MessageNotification, ...]]:
        return comms.views.message_notifications_for_references(self.references)

    def changed_since(self, comms: Comms, revision: WireRevision,
                      previous: NotificationsRead) -> bool:
        return previous.live_changed(comms, revision)


@dataclass(frozen=True)
class ReadRecordedNotifications(ReadNotifications):
    """The selected archive's recorded handling, independent of today's recipients."""

    source: str

    def read(self, comms: Comms) -> Mapping[tuple[int, str], tuple[MessageNotification, ...]]:
        from agent_comms.historical_views import HistorySource

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
    activity: Mapping[str, ObservedActivity | None] = field(default_factory=dict)

    @classmethod
    def of(cls, revision: WireRevision, comms: Comms, known: Mapping[str, ObservedActivity],
           outcomes: Mapping[tuple[int, str], tuple[MessageNotification, ...]],
           ) -> NotificationsRead:
        names = {name for items in outcomes.values()
                 for item in items for name in item.activity_names()}
        known = {**observed_activity(comms, names - known.keys()), **known}
        return cls(revision, {name: known.get(name) for name in names})

    def live_changed(self, comms: Comms, revision: WireRevision) -> bool:
        """The whole window changed, or one of its participants' activity did."""
        if revision.changed_since(NotificationsView(), self.revision):
            return True
        current = observed_activity(comms, self.activity) if self.activity else {}
        return any(current.get(name) != seen for name, seen in self.activity.items())


class UnreadableNotificationsRead(NotificationsRead):
    """An unreadable read names no participants, so any later revision reads again."""

    def live_changed(self, comms: Comms, revision: WireRevision) -> bool:
        return revision != self.revision


def observed_activity(comms: Comms, names: Iterable[str]) -> dict[str, ObservedActivity]:
    """Each named thread's current activity, keyed by the name it was asked for."""
    snapshot = comms.registry.snapshot()
    names = frozenset(names)
    captured = comms.agents.all_activity(
        snapshot.visible_threads(DEFAULT_ROSTER, names=names), snapshot=snapshot)
    return {name: captured[canonical] for name in names
            if (canonical := snapshot.canonical_name(name)) in captured}


@dataclass(frozen=True)
class ReadOwnerGoal(OwnerRead):
    """The owner's goal snapshot and the turn it read with it."""

    async def read(self, owner: RuntimeConnection) -> GoalSnapshotReply:
        from agent_comms.acp_extension import GoalSnapshotReply

        return FieldCodec.decode(GoalSnapshotReply, await owner.request("goal_snapshot"))


@dataclass(frozen=True)
class ReadInputDelivery(OwnerRead):
    """The owner's delivery notices, checked against its live queue."""

    include_history: bool = False

    async def read(self, owner: RuntimeConnection) -> InputDelivery:

        return InputDelivery.from_wire(
            await owner.request("input_dispositions", include_history=self.include_history))


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
        )).continued().answer(  # The sidebar runs the next bounded preparation unit itself.
            lambda observed: observed, lambda: SidebarDeferred(revision, self),
            lambda error: SidebarUnreadable(revision, self, error))


class SidebarReadStage:
    def read(
        self, request: ObserveSidebar, comms: Comms, revision: WireRevision
    ) -> SidebarResult:
        return request._answer(revision, comms.views.viewer_snapshot, request.worktree)

    def next(self) -> SidebarReadStage:
        return self


class RosterSidebarRead(SidebarReadStage):
    def read(
        self, request: ObserveSidebar, comms: Comms, revision: WireRevision
    ) -> SidebarResult:
        viewer = comms.messaging.user_identity(request.worktree).name
        return request._answer(revision, comms.views.presentation.roster, viewer)

    def next(self) -> SidebarReadStage:
        return SidebarReadStage()


class SidebarReading(Observation[ObserveSidebar]):
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

    def read(self, comms: Comms, revision: WireRevision) -> Iterator[ObservationStep]:
        while True:
            key = SidebarSent(revision.expiry_period, type(self.stage), revision)
            if not self.sent.changed_since(key):
                return
            stage = self.stage
            try:
                result = stage.read(self.request, comms, revision)
            except RouteIndexRebuilding:
                # Continue bounded preparation, letting the service accept and
                # serve foreground requests between every bounded unit.
                yield ObservationPending()
                continue
            yield from result.finish_read(self, key)
            if self.stage is stage:
                return

    def publish(self, result: SidebarResult, key: SidebarSent) -> Iterator[SidebarResult]:
        self.sent = key
        result.advance(self)
        if not self.published.same_observation(result):
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


class NotificationReading(Observation[ObserveNotifications]):
    def __init__(self, request: ObserveNotifications):
        super().__init__(request)
        self.read_states: dict[ReadNotifications, NotificationsRead] = {}

    def read(self, comms: Comms, revision: WireRevision) -> Iterator[ObservationStep]:
        for request in self.request.reads:
            previous = self.read_states.get(request)
            if previous is not None and not request.changed_since(comms, revision, previous):
                continue
            yield self.notifications(request, comms, revision, previous)

    def notifications(self, request: ReadNotifications, comms: Comms, revision: WireRevision,
                      previous: NotificationsRead | None) -> ObservationStep:
        known = observed_activity(comms, () if previous is None else previous.activity)

        def read() -> Mapping[tuple[int, str], tuple[MessageNotification, ...]]:
            request.require_root(comms)
            return request.read(comms)

        def settled(result: Observed, state: NotificationsRead) -> Observed:
            self.read_states[request] = state
            return result
        return UnreadableHistory.outcome(read).answer(
            lambda outcomes: settled(NotificationsObserved(revision, request, outcomes),
                                     NotificationsRead.of(revision, comms, known, outcomes)),
            ObservationPending,  # Keep the interest pending until its source can be read.
            lambda error: settled(NotificationsUnreadable(revision, request, error),
                                  UnreadableNotificationsRead(revision)))


class ThreadReading(Observation[ObserveThreads]):
    def __init__(self, request: ObserveThreads):
        super().__init__(request)
        self.observed = ThreadsObserved(WireRevision.empty(), {}, frozenset(), {})
        self.initial = request.names

    def continue_from(self, previous: Self) -> Self:
        self.observed = previous.observed
        self.initial = self.request.names - previous.request.names
        return self

    def read(self, comms: Comms, revision: WireRevision) -> Iterator[ObservationStep]:
        previous = self.observed
        results: dict[str, ThreadReadResult] = {}
        activity: dict[str, ObservedActivity] = {}
        names = self.request.names

        def located() -> dict[str, Callable[[], ThreadPresentation | None]]:
            nonlocal activity, names
            readable, _ = previous.revision_inputs()
            watched = names | frozenset(actor for view in readable.values()
                                        for actor in view.notification_activity_names())
            activity = observed_activity(comms, watched)
            names = (revision.changed_threads(previous, comms, activity) | names - previous.results.keys()) & names
            if not names:
                return {}
            batch = comms.views.thread_presentations(names)
            return {name: partial(batch.read, name) for name in names}

        def failed(result: ThreadReadResult) -> Iterator[ObservationStep]:
            results.update(dict.fromkeys(names, result))
            return iter(())

        yield from UnreadableHistory.outcome(located).answer(
            lambda reads: self.presented(reads, results),
            lambda: failed(ThreadBusy()), lambda error: failed(ThreadUnavailable(error)))
        results.update((name, value) for name, value in previous.results.items()
                       if name in self.request.names and name not in names)
        observed = ThreadsObserved(revision, results, frozenset(), activity)
        self.observed = observed
        changed = observed.changed_since(previous) | self.initial
        self.initial = frozenset()
        if changed:
            yield replace(observed, changed=changed,
                          results={name: value for name, value in results.items() if name in changed})

    @staticmethod
    def presented(reads: Mapping[str, Callable[[], ThreadPresentation | None]],
                  results: dict[str, ThreadReadResult]) -> Iterator[ObservationStep]:
        for name, read in reads.items():
            results[name] = UnreadableHistory.outcome(read).answer(
                lambda shown: ThreadMissing() if shown is None else ThreadPresented(shown),
                ThreadBusy, ThreadUnavailable)
            yield ObservationPending()


class ViewReading(Observation[ObserveViews]):
    def __init__(self, request: ObserveViews):
        super().__init__(request)
        self.revision: ReadState = NoRevision()

    def read(self, comms: Comms, revision: WireRevision) -> Iterator[ObservationStep]:
        if not self.revision.changed_since(RegistrationsView(), revision):
            return
        retired = comms.views.retired_views(self.request.threads, self.request.channels)
        self.revision = revision
        if retired.threads or retired.channels:
            yield ViewsRetired(revision, retired)


class ServiceResult(ABC):
    """What the service sends; each declares how the UI side receives it."""

    @abstractmethod
    def received(self, process: ObservationProcess, observed: list[Observed]) -> None: ...


@dataclass(frozen=True)
class Observed(ObservationStep, ServiceResult):
    """A result, as of one revision of Core's stores."""

    revision: WireRevision

    def publish(self, service: ObservationService) -> None:
        service.send(self)

    def received(self, process: ObservationProcess, observed: list[Observed]) -> None:
        observed.append(self)


@dataclass(frozen=True)
class ReadAnswered(ServiceResult):
    request_id: int
    value: object

    def received(self, process: ObservationProcess, observed: list[Observed]) -> None:
        if (waiter := process.reads.pop(self.request_id, None)) is not None and not waiter.done():
            waiter.set_result(self.value)


@dataclass(frozen=True)
class ReadBusy(ServiceResult):
    """Source busy: the original read remains pending, not a failed input."""

    request_id: int

    @property
    def title(self) -> str:
        return "History loading"

    def received(self, process: ObservationProcess, observed: list[Observed]) -> None:
        # Keep the same waiter. The service owns waiting for the source and
        # answers this request when its next observation can acquire a read.
        pass


@dataclass(frozen=True)
class ReadRefused(ServiceResult):
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


class NoSidebarResult(PublishState):
    pass


@dataclass(frozen=True)
class SidebarResult(PublishState, Observed):
    revision: WireRevision = field(compare=False)
    request: ObserveSidebar

    def finish_read(self, reading: SidebarReading, key: SidebarSent) -> Iterator[SidebarResult]:
        return reading.publish(self, key)

    def advance(self, reading: SidebarReading) -> None:
        """Keep this stage until its source revision changes."""


@dataclass(frozen=True)
class SidebarDeferred(SidebarResult):
    """Keep the original interest, stage and published answer for the next read."""

    def finish_read(self, reading: SidebarReading, key: SidebarSent) -> Iterator[SidebarResult]:
        return iter(())


@dataclass(frozen=True)
class SidebarObserved(SidebarResult):
    snapshot: CoordinationSnapshot
    derived: tuple[dict[str, ChannelRowModel], dict[str, ThreadRowModel]]

    def advance(self, reading: SidebarReading) -> None:
        reading.stage = reading.stage.next()
        if self.snapshot.unread_pending:
            reading.sent = NoSidebarSent()


@dataclass(frozen=True)
class SidebarUnreadable(SidebarResult):
    """The requested sidebar could not be read; no stale snapshot is offered."""

    error: UnreadableHistory


class ThreadReadResult(PublishState, ABC):
    """One thread's last read, including how to compare and display its answer."""

    @abstractmethod
    def status_row(self, name: str, owner: OpenThreadStatus) -> ThreadStatusModel: ...

    def collect_revision_inputs(self, name: str, readable: dict[str, ThreadPresentation], retry: set[str]) -> None:
        """Supply readable values and reads that must be tried again."""

    def same_status(self, presentation: ThreadPresentation) -> bool:
        return False


@dataclass(frozen=True)
class ThreadPresented(ThreadReadResult):
    presentation: ThreadPresentation

    def same_observation(self, previous: object) -> bool:
        return isinstance(previous, ThreadPresented) and (
            self.presentation is previous.presentation or self.presentation.same_observation(previous.presentation))

    def status_row(self, name: str, owner: OpenThreadStatus) -> ThreadStatusModel:
        return ThreadStatusModel.known(name, self.presentation)

    def collect_revision_inputs(self, name: str, readable: dict[str, ThreadPresentation], retry: set[str]) -> None:
        readable[name] = self.presentation

    def same_status(self, presentation: ThreadPresentation) -> bool:
        return presentation.same_status(self.presentation)


@dataclass(frozen=True)
class ThreadMissing(ThreadReadResult):
    def status_row(self, name: str, owner: OpenThreadStatus) -> ThreadStatusModel:
        return ThreadStatusModel.retired(name)


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
    activity: Mapping[str, ObservedActivity]

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
class ViewsRetired(Observed):
    """Open views whose thread was deleted or replaced, or whose channel is gone or archived."""

    retired: RetiredViews


@dataclass(frozen=True)
class ObservationFailed(ServiceResult):
    """The service failed and is exiting; ``error`` is its traceback."""

    error: str

    def received(self, process: ObservationProcess, observed: list[Observed]) -> None:
        raise process.ended(self.error)


class ObserverDied(CoordinationError):
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


class ObservationService:
    """The child process side: one Comms service, the UI's interest, and what was sent."""

    def __init__(self, comms, connection: Connection):
        self.comms = comms
        self.connection = connection
        self.interest: ObservationInterest = LiveObservationInterest()
        self.changed = asyncio.Event()
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
        from agent_comms.wire_watch import ModifiedWatchSource, WireWatch

        paths = tuple(path for group in WireRevision.source_paths(self.comms.views).values()
                      for path in group)
        sources = tuple(ModifiedWatchSource(directory, frozenset(
            os.fsencode(path.name) for path in paths if path.parent == directory))
            for directory in dict.fromkeys(path.parent for path in paths))
        async for _ in WireWatch.observations(*sources):
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
            if not isinstance(answer, ReadBusy):
                self.pending.pop(request_id)
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

    async def run(self) -> None:
        loop = asyncio.get_running_loop()
        # Request and watcher defects belong to the service process boundary;
        # an unobserved background-task error must not strand an awaited UI read.
        async with self.tasks:
            loop.add_reader(self.connection.fileno(), self.receive)
            watching = self.tasks.create_task(self.watch())
            expiring = self.tasks.create_task(self.expire())
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
                self.pending.clear()
                self.comms.views.transcript_reads.close()


def serve(route: CommsRoute, connection: Connection) -> None:
    """Observe the caller's selected route, including its reviewed native package."""
    from agent_comms.comms import wire

    try:
        with route.hold_client_route():
            asyncio.run(ObservationService(wire(route), connection).run())
    except BaseException:
        try:
            connection.send(ObservationFailed(traceback.format_exc()))
        except OSError:
            pass
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
            loop.add_reader(descriptor, process.results)
        except BaseException:
            process.close()
            raise
        process._reader.callback(loop.remove_reader, descriptor)
        return process

    def __init__(self, route: CommsRoute):
        self._resources = ExitStack()
        self.request_ids = itertools.count(1)
        self.reads: dict[int, asyncio.Future] = {}
        # Set by ``results`` when it reads that the service ended.
        self.died: ObserverDied | None = None
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
        """Tell the service what the UI shows now; a service that ended is not told.

        Its end is reported by ``results`` alone. A pipe that breaks here
        belongs to a service ``results`` has not read the end of yet: the
        same closed pipe is readable there.
        """
        if self.died is None and not self.connection.closed:
            try:
                self.connection.send(request)
            except BrokenPipeError:
                pass

    def ended(self, error: str | None) -> ObserverDied:
        """The service ended (``error``: the traceback it sent): fail its awaited reads and say how it exited."""
        self._reader.close()
        self.process.join(timeout=2)
        self.died = ObserverDied(self.process.pid, self.process.exitcode, error)
        self.abandon_reads(self.died)
        return self.died

    async def read(self, request: AwaitedRead):
        """Send ``request`` and await its answer, delivered by ``results``.

        Cancelling the caller cancels the read; an unserved read is dropped.
        """
        if self.died is not None:
            raise self.died
        if self.connection.closed:
            raise StaleRevision("Cannot read a retired Comms observation process")
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

    def close(self) -> None:
        self._resources.close()
