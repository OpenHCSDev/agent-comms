"""Server-side observation service, per-interest readings, and local child launch.

Client code imports wire records and ObservationProcess from
``agent_comms.ui_model.observation`` without pulling in Comms, HistoryViews,
NotificationAssignment, or TeamBoardPublicationStore.
"""

from __future__ import annotations

import asyncio
import math
import socket
import time
import traceback
from abc import ABC, abstractmethod
from collections.abc import Callable, Generator, Iterator, Mapping
from concurrent.futures import Future
from contextlib import AbstractContextManager, ExitStack, aclosing, nullcontext
from dataclasses import dataclass, field, replace
from functools import cached_property, partial
from multiprocessing.connection import Connection
from typing import ClassVar, Self

from metaclass_registry.mro_dispatch import MroDispatch, handles

from agent_comms.comms import Comms
from agent_comms.execution.child_process import HelperStart, ProcessName, SpawnedHelper
from agent_comms.execution.launch_root import started
from agent_comms.history_views import SidebarCut
from agent_comms.installer.active_route import CommsRoute
from agent_comms.messaging.message_notification import NotificationPass, NotificationWindow
from agent_comms.messaging.revision_views import (
    NotificationsView,
    RegistrationsView,
    RetainedTeamFramesView,
    RevisionView,
    RosterView,
    SidebarBasisView,
    SidebarView,
    StatusView,
    StoresView,
)
from agent_comms.messaging.watched import FileSource
from agent_comms.nominal.field_codec import MessageId
from agent_comms.notification_assignment import NotificationAssignment
from agent_comms.notification_read import (
    NotificationsRead,
    StaleNotificationsRead,
    UnreadableNotificationsRead,
)
from agent_comms.planning.goal_presentation import GoalExecutionControls
from agent_comms.planning.goal_waits import GoalWaits
from agent_comms.presentation import (
    CoordinationSnapshot,
    NoRevision,
    ReadState,
    WireRevision,
)
from agent_comms.thread_views import ThreadView
from agent_comms.registry.activity import ObservedActivity
from agent_comms.registry.registry_document import RegistrySnapshot, UnreadRegistry
from agent_comms.registry.registry_names import DeletedThread
from agent_comms.registry.thread_presentation import ThreadPresentation
from agent_comms.registry.view_unread import ReplyIndexResult
from agent_comms.storage.locked_store import LockedStore
from agent_comms.storage.typed_table import TypedTable
from agent_comms.team.team_board_selection import TeamBoardSelection
from agent_comms.team.team_hud import TeamBoardPublicationStore
from agent_comms.ui_model.observation import (
    CancelRead,
    InterestRequest,
    NoSidebarResult,
    NotificationAnswer,
    NotificationsObserved,
    NotificationsReload,
    NotificationsUnreadable,
    ObservationFailed,
    ObservationHost,
    ObservationProcess,
    ObservationRequest,
    ObservationStep,
    Observed,
    ObserveNotifications,
    ObserveSidebar,
    ObserveTeamHud,
    ObserveThreads,
    ObserveViews,
    OwnerRead,
    PublishState,
    ReadBusy,
    ReadNotifications,
    ReadReload,
    ReadRequest,
    ReadUnreadable,
    ReadValue,
    RetryableRead,
    RevisionObserved,
    ServiceResult,
    SidebarObserved,
    SidebarResult,
    StopObserving,
    StopSidebar,
    StopTeamHud,
    TeamHudObserved,
    TeamHudUnreadable,
    ThreadBusy,
    ThreadPresented,
    ThreadReadResult,
    ThreadsObserved,
    ThreadUnavailable,
    UnreadableHistory,
    ViewerEnd,
    ViewsRetired,
)
from agent_comms.ui_model.observation_wire import (
    EncodedConnection,
    ObservationPeer,
    ObservationSource,
    ObserverDied,
    ObserverProcessDied,
)


class ObservationInterest(ABC):
    @abstractmethod
    def receive(self, observation: Observation, service: ObservationService) -> None: ...

    @abstractmethod
    def stop(self) -> ObservationInterest: ...

    def remove(self, kind: type[Observation]) -> None:
        pass

    def prepared(self, observation: ReadingObservation, service: ObservationService) -> None:
        pass

    async def observe(
        self, service: ObservationService, revision: WireRevision, attempted: set[int],
    ) -> None:
        pass


class StoppedObservationInterest(ObservationInterest):
    def receive(self, observation: Observation, service: ObservationService) -> None:
        raise RuntimeError("The observer has stopped accepting interests")

    def stop(self) -> ObservationInterest:
        return self


class LiveObservationInterest(ObservationInterest):
    def __init__(self) -> None:
        self.observations: dict[type[Observation], Observation] = {}

    def receive(self, observation: Observation, service: ObservationService) -> None:
        kind = type(observation)
        self.observations[kind] = self.observations.get(
            kind, ObservationLifetime(),
        ).for_interest(observation)
        service.observe_again()

    def stop(self) -> ObservationInterest:
        for observation in self.observations.values():
            observation.close()
        self.observations.clear()
        return StoppedObservationInterest()

    def remove(self, kind: type[Observation]) -> None:
        self.observations.pop(kind, ObservationLifetime()).close()

    def prepared(self, observation: ReadingObservation, service: ObservationService) -> None:
        if self.observations.get(type(observation)) is observation:
            observation.invalidate()
            service.observe_again()

    async def observe(
        self, service: ObservationService, revision: WireRevision, attempted: set[int],
    ) -> None:
        observations = tuple(sorted(
            self.observations.items(), key=lambda item: item[1].priority,
        ))
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


class Observation[InterestT: InterestRequest](ObservationLifetime):
    """Retire a revision's vanished sources before presenting its observations."""

    priority: ClassVar[int] = 1

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
            result.continue_stepping(service)

    @abstractmethod
    def read(
        self, comms: Comms, revision: WireRevision,
    ) -> Generator[ObservationStep]: ...

    def after_read(self, service: ObservationService) -> None:
        """Subscribe to completion when this observation has unfinished preparation."""


@dataclass(frozen=True)
class RetryObservationStep(ObservationStep):
    """A store read hit transient contention: invalidate this reading and retry after busy pause."""

    reading: ReadingObservation

    def publish(self, host: ObservationHost) -> None:
        self.reading.invalidate()
        host.mark_busy()

    def continue_stepping(self, host: ObservationHost) -> None:
        """Wait for retry_busy to schedule the next pass instead of tight-spinning."""


class HeldSidebarCut(ABC):
    """What a sidebar stage holds from its last full read."""

    @abstractmethod
    def read(
        self, stage: SidebarReadStage, request: ObserveSidebar, comms: Comms,
        revision: WireRevision,
    ) -> SidebarResult: ...


class NoSidebarCut(HeldSidebarCut):
    def read(
        self, stage: SidebarReadStage, request: ObserveSidebar, comms: Comms,
        revision: WireRevision,
    ) -> SidebarResult:
        return stage.read_cut(request, comms, revision)


@dataclass(frozen=True)
class SidebarCutAt(HeldSidebarCut):
    """A cut and the revision it was read at. A revision that moved none of its counted stores,
    and a registry that moved only turns and presence, moved what the rows read: activity, goals,
    relationships and turns. Changed roster sources refresh the rows over the same counts,
    without reopening the bus, recounting unread or reading native transcripts."""

    cut: SidebarCut
    revision: WireRevision

    def read(
        self, stage: SidebarReadStage, request: ObserveSidebar, comms: Comms,
        revision: WireRevision,
    ) -> SidebarResult:
        if revision.changed_since(SidebarBasisView(), self.revision):
            return stage.read_cut(request, comms, revision)
        registry = comms.registry.snapshot()
        if not registry.moved_turns_alone(self.cut.registry):
            return stage.read_cut(request, comms, revision)
        stage.held = held = SidebarCutAt(replace(self.cut, registry=registry), revision)
        return request._answer(revision, partial(stage.snapshot, held.cut, comms, revision))


class SidebarReadStage:
    """After the first roster read, each read holds the cut it took. Every sidebar store feeds
    SidebarView, so a revision that reaches this stage moved one; SidebarBasisView says whether
    it was one of the cut's."""

    def __init__(self) -> None:
        self.held: HeldSidebarCut = NoSidebarCut()
        self.roster_revision: ReadState = NoRevision()
        self.rows: tuple[ThreadView, ...] = ()

    def read(
        self, request: ObserveSidebar, comms: Comms, revision: WireRevision,
    ) -> SidebarResult:
        return self.held.read(self, request, comms, revision)

    def read_cut(
        self, request: ObserveSidebar, comms: Comms, revision: WireRevision,
    ) -> SidebarResult:
        def read() -> CoordinationSnapshot:
            cut = comms.views.viewer_cut(request.worktree, request.visibility)
            self.held = SidebarCutAt(cut, revision)
            return self.snapshot(cut, comms, revision)

        return request._answer(revision, read)

    def snapshot(
        self, cut: SidebarCut, comms: Comms, revision: WireRevision,
    ) -> CoordinationSnapshot:
        """A count-only change reuses rows acquired under their original source revision."""
        if revision.changed_since(RosterView(), self.roster_revision):
            rows = ThreadView.roster(cut.registry, comms.agents, comms.goals, cut.visibility)
            self.rows, self.roster_revision = rows, revision
        return cut.present(comms.root, self.rows)

    def advance(self, reading: SidebarReading, snapshot: CoordinationSnapshot) -> None:
        """A full read waits for a changed source or its native-index watcher."""


class RosterSidebarRead(SidebarReadStage):
    """The first read: rows without whole-bus derived indexes; the next read takes a cut."""

    def read(
        self, request: ObserveSidebar, comms: Comms, revision: WireRevision,
    ) -> SidebarResult:
        viewer = comms.messaging.user_identity(request.worktree).name
        return request._answer(
            revision, partial(comms.views.presentation.roster, viewer, request.visibility))

    def advance(self, reading: SidebarReading, snapshot: CoordinationSnapshot) -> None:
        reading.stage = SidebarReadStage()
        reading.invalidate()


class SidebarReading(ReadingObservation[ObserveSidebar], MroDispatch):
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

    @handles(SidebarObserved)
    def advance_observed(self, result: SidebarObserved) -> None:
        self.stage.advance(self, result.snapshot)

    @handles(SidebarResult)
    def advance_other(self, result: SidebarResult) -> None:
        """Non-observed sidebar results keep the current stage until the source revision moves."""

    def publish(self, result: SidebarResult) -> Iterator[SidebarResult]:
        self.dispatch_sync(result)
        if result.accepted_by(self.published):
            sent = self.published.sent_as(result)
            self.published = result
            yield sent


class _TeamHudReadOutcome(MroDispatch):
    @handles(ReadValue)
    def observed(
        self, outcome: ReadValue[TeamHudObserved], revision: WireRevision,
        reading: ReadingObservation,
    ) -> ObservationStep:
        return outcome.value

    @handles(RetryableRead)
    def retrying(
        self, outcome: RetryableRead[TeamHudObserved], revision: WireRevision,
        reading: ReadingObservation,
    ) -> ObservationStep:
        return RetryObservationStep(reading)

    @handles(ReadUnreadable)
    def unreadable(
        self, outcome: ReadUnreadable[TeamHudObserved], revision: WireRevision,
        reading: ReadingObservation,
    ) -> ObservationStep:
        return TeamHudUnreadable(revision, outcome.cause)


_TEAM_HUD_READ_OUTCOME = _TeamHudReadOutcome()


@dataclass(frozen=True)
class RetainedTeamFrames:
    """The heartbeat's retained atomic publication for one reader selection."""

    selection: TeamBoardSelection

    def observed(
        self, comms: Comms, revision: WireRevision, reading: ReadingObservation,
    ) -> Generator[ObservationStep]:
        outcome = UnreadableHistory.outcome(
            lambda: TeamHudObserved(
                revision,
                TeamBoardPublicationStore.read_cut(comms.root, self.selection),
            ),
        )
        yield _TeamHudReadOutcome.handler_for(type(outcome))(
            _TEAM_HUD_READ_OUTCOME, outcome, revision, reading,
        )


class TeamHudReading(ReadingObservation[ObserveTeamHud]):
    """Read the heartbeat publication; views never acquire the board themselves."""

    view = RetainedTeamFramesView()

    def prepare(
        self, comms: Comms, revision: WireRevision,
    ) -> Generator[ObservationStep]:
        """Consume the root-owned atomic publication for this selection."""
        return RetainedTeamFrames(self.request.selection).observed(comms, revision, self)

    def read(
        self, comms: Comms, revision: WireRevision,
    ) -> Generator[ObservationStep]:
        return self.prepare(comms, revision)


class BatchRead(ABC):
    """What one attempt to read a batch of references produced."""

    @abstractmethod
    def settled(
        self, family: ReadNotifications, current: list[NotificationBatch],
        pending: set[ReadNotifications],
    ) -> Iterator[ObservationStep]:
        """Record this attempt among the reading's batches, yielding any step it owes now."""


class PendingBatch(BatchRead):
    """The store could not be read yet; the family's requests wait for retry_busy."""

    def settled(
        self, family: ReadNotifications, current: list[NotificationBatch],
        pending: set[ReadNotifications],
    ) -> Iterator[ObservationStep]:
        pending.add(family)
        return iter(())


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

    def settled(
        self, family: ReadNotifications, current: list[NotificationBatch],
        pending: set[ReadNotifications],
    ) -> Iterator[ObservationStep]:
        current.append(self)
        return iter(())


class _NotificationReadOutcome(MroDispatch):
    @handles(ReadValue)
    def observed(
        self, outcome: ReadValue[NotificationWindow], request: ReadNotifications,
        comms: Comms, revision: WireRevision, activity,
    ) -> BatchRead:
        return NotificationBatch(
            request, NotificationsRead.of(revision, comms, activity, outcome.value),
            NotificationsObserved(revision, request, outcome.value.outcomes),
        )

    @handles(RetryableRead)
    def deferred(
        self, outcome: RetryableRead[NotificationWindow], request: ReadNotifications,
        comms: Comms, revision: WireRevision, activity,
    ) -> BatchRead:
        return PendingBatch()

    @handles(ReadReload)
    def reload(
        self, outcome: ReadReload[NotificationWindow], request: ReadNotifications,
        comms: Comms, revision: WireRevision, activity,
    ) -> BatchRead:
        # The request's own display cut is stale: answer it, so the UI asks afresh.
        return NotificationBatch(
            request, StaleNotificationsRead(revision),
            NotificationsReload(revision, request, outcome.error),
        )

    @handles(ReadUnreadable)
    def unreadable(
        self, outcome: ReadUnreadable[NotificationWindow], request: ReadNotifications,
        comms: Comms, revision: WireRevision, activity,
    ) -> BatchRead:
        return NotificationBatch(
            request, UnreadableNotificationsRead(revision),
            NotificationsUnreadable(revision, request, outcome.cause),
        )


class _LocatedThreadsOutcome(MroDispatch):
    @handles(ReadValue)
    def observed(
        self, outcome: ReadValue[LocatedThreads], reading: ThreadReading,
        located: LocatedThreads, results: dict[str, ThreadReadResult],
    ) -> Iterator[ObservationStep]:
        return reading.presented(outcome.value.reads, results)

    @handles(RetryableRead)
    def busy(
        self, outcome: RetryableRead[LocatedThreads], reading: ThreadReading,
        located: LocatedThreads, results: dict[str, ThreadReadResult],
    ) -> Iterator[ObservationStep]:
        return reading.failed(located.names, ThreadBusy(), results)

    @handles(ReadUnreadable)
    def unreadable(
        self, outcome: ReadUnreadable[LocatedThreads], reading: ThreadReading,
        located: LocatedThreads, results: dict[str, ThreadReadResult],
    ) -> Iterator[ObservationStep]:
        return reading.failed(located.names, ThreadUnavailable(outcome.cause), results)


class _ThreadPresentationOutcome(MroDispatch):
    @handles(ReadValue)
    def presented(self, outcome: ReadValue[ThreadPresentation]) -> ThreadReadResult:
        return ThreadPresented(outcome.value)

    @handles(RetryableRead)
    def busy(self, outcome: RetryableRead[ThreadPresentation]) -> ThreadReadResult:
        return ThreadBusy()

    @handles(ReadUnreadable)
    def unreadable(self, outcome: ReadUnreadable[ThreadPresentation]) -> ThreadReadResult:
        return ThreadUnavailable(outcome.cause)


_NOTIFICATION_READ_OUTCOME = _NotificationReadOutcome()
_LOCATED_THREADS_OUTCOME = _LocatedThreadsOutcome()
_THREAD_PRESENTATION_OUTCOME = _ThreadPresentationOutcome()


class NotificationReading(ReadingObservation[ObserveNotifications]):
    """Answers each visible-set request from the batches that cover it, and reads only the
    references no current batch covers. Scrolling therefore reads the newly visible rows,
    not every visible row again."""

    priority = 0
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
        self.batches = tuple(
            batch for batch in previous.batches
            if batch.keys & wanted.get(batch.request.family, frozenset())
        )
        self.answered = {
            request: batches for request, batches in previous.answered.items()
            if request in self.request.reads
        }
        return self

    def read(self, comms: Comms, revision: WireRevision) -> Generator[ObservationStep]:
        current, stale = [], []
        notifications = NotificationPass(comms, revision)
        notifications.prepare(
            (batch.state.notification_window, batch.state.revision)
            for batch in self.batches
        )
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
        if pending:
            yield RetryObservationStep(self)

    def batch(
        self, request: ReadNotifications, comms: Comms, revision: WireRevision,
        known: frozenset[str],
    ) -> BatchRead:
        activity = comms.views.observed_activity(revision, known)

        def read() -> NotificationWindow:
            request.require_root(comms)
            return request.read(comms)

        outcome = UnreadableHistory.outcome(read)
        return _NotificationReadOutcome.handler_for(type(outcome))(
            _NOTIFICATION_READ_OUTCOME, outcome, request, comms, revision, activity,
        )

    def answer(self, request: ReadNotifications, revision: WireRevision) -> Iterator[Observed]:
        """Send a request its outcomes only when a batch behind them was read since."""
        keys = frozenset(request.keys)
        sources = tuple(
            batch for batch in self.batches
            if batch.request.family == request.family and batch.keys & keys
        )
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
    reads: AbstractContextManager[Mapping[str, Callable[[], ThreadPresentation]]]
    painted: Mapping[str, ThreadReadResult] = field(default_factory=dict)


class ThreadReading(ReadingObservation[ObserveThreads]):
    view = StatusView()

    def __init__(self, request: ObserveThreads):
        super().__init__(request)
        self.observed = ThreadsObserved(WireRevision.empty(), {}, frozenset(), {})
        self.initial = request.threads
        self.located_threads = LocatedThreads({}, request.threads, nullcontext({}))
        self.registry_read: RegistrySnapshot | UnreadRegistry = UnreadRegistry()
        # Each open thread's goal state at the last goal-store change: a goal write
        # re-reads the status of the threads whose goal it changed, not every open thread.
        self.goals: Mapping[str, GoalExecutionControls] = {}
        self.goals_read = WireRevision.empty().goal_execution

    def continue_from(self, previous: Self) -> Self:
        self.observed = previous.observed
        self.registry_read = previous.registry_read
        self.goals, self.goals_read = previous.goals, previous.goals_read
        self.initial = self.request.threads - previous.request.threads
        return self

    def read(self, comms: Comms, revision: WireRevision) -> Generator[ObservationStep]:
        previous = self.observed
        results: dict[str, ThreadReadResult] = {}
        outcome = UnreadableHistory.outcome(partial(self.located, comms, revision, previous))
        located = self.located_threads
        yield from _LocatedThreadsOutcome.handler_for(type(outcome))(
            _LOCATED_THREADS_OUTCOME, outcome, self, located, results,
        )
        retained = {**previous.results, **located.painted}
        results.update(
            (name, value) for name, value in retained.items()
            if name in self.request.threads and name not in located.names
        )
        observed = ThreadsObserved(revision, results, frozenset(), located.activity)
        self.observed = observed
        changed = observed.changed_since(previous) | self.initial
        self.initial = frozenset()
        if changed:
            # The reader keeps its acquired results for the next change check; a viewer
            # receives only each changed result's published projection.
            yield replace(
                observed,
                changed=changed,
                results={
                    name: value.published() for name, value in results.items()
                    if name in changed
                },
            )

    def located(
        self, comms: Comms, revision: WireRevision, previous: ThreadsObserved,
    ) -> LocatedThreads:
        # Keep each completed stage when a later stored read fails, exactly as
        # the original read did before its activity/names became declared state.
        self.located_threads = LocatedThreads({}, self.request.threads, nullcontext({}))
        readable, _ = previous.revision_inputs()
        names = self.located_threads.names
        watched = names | frozenset(
            actor for view in readable.values()
            for actor in view.notification_window.activity_names()
        )
        activity = comms.views.observed_activity(revision, watched)
        self.located_threads = replace(self.located_threads, activity=activity)
        snapshot = comms.registry.snapshot()
        registry_changed = self.registry_read.changed_names_to(snapshot)
        transcript_sources_changed = self.registry_read.transcript_sources_changed_to(
            snapshot, names,
        )
        goals_changed = self.goals_changed(comms, revision, snapshot)
        changed = previous.changed_threads(
            revision, comms, activity, snapshot, registry_changed,
            transcript_sources_changed, goals_changed,
        )
        self.registry_read = snapshot
        names = (changed.reread | names - previous.results.keys()) & names
        self.located_threads = replace(
            self.located_threads, names=names, painted=changed.painted,
        )
        if names:
            batch = comms.views.thread_presentations(names)
            self.located_threads = replace(
                self.located_threads, reads=batch.readings(names),
            )
        return self.located_threads

    def goals_changed(
        self, comms: Comms, revision: WireRevision, snapshot: RegistrySnapshot,
    ) -> frozenset[str]:
        """The open threads whose goal state moved since the goal store last changed."""
        if revision.goal_execution == self.goals_read and self.goals:
            return frozenset()
        threads = tuple(
            snapshot.threads[name] for name in self.request.threads
            if name in snapshot.threads
        )
        waits = partial(GoalWaits.for_goal, rows=comms.goals.waits.read())
        goals = {
            thread.name: goal.execution for thread, goal in zip(
                threads, comms.goals.snapshots_for(threads, snapshot, waits, comms.agents),
                strict=True,
            )
        }
        changed = frozenset(name for name, goal in goals.items() if self.goals.get(name) != goal)
        self.goals, self.goals_read = goals, revision.goal_execution
        return changed

    @staticmethod
    def failed(
        names: frozenset[str], result: ThreadReadResult,
        results: dict[str, ThreadReadResult],
    ) -> Iterator[ObservationStep]:
        results.update(dict.fromkeys(names, result))
        return iter(())

    @staticmethod
    def presented(
        reads: AbstractContextManager[Mapping[str, Callable[[], ThreadPresentation]]],
        results: dict[str, ThreadReadResult],
    ) -> Iterator[ObservationStep]:
        with reads as captured:
            for name, read in captured.items():
                outcome = UnreadableHistory.outcome(read)
                results[name] = _ThreadPresentationOutcome.handler_for(type(outcome))(
                    _THREAD_PRESENTATION_OUTCOME, outcome,
                )
                yield ObservationStep()


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


class ObservationService(ObservationHost, MroDispatch):
    """The child process side: one Comms service, the UI's interest, and what was sent."""

    def __init__(self, comms: Comms, connection: Connection):
        self.comms = comms
        self.connection = connection
        self.interest: ObservationInterest = LiveObservationInterest()
        self.changed = asyncio.Event()
        # A read answered busy, until the service tries the pending reads again.
        self.busy = asyncio.Event()
        self.revision: ReadState = NoRevision()
        self.observe_due = True
        # Stores commit in storms while agents take turns. A pass adopts a new revision only
        # once the previous revision's readings have finished and observe_gap has passed, so
        # a storm costs a few passes a second instead of one after another.
        self.stepping = False
        self.unsettled = False
        self.settled_at = -math.inf
        self.adoption_owed = False
        self.adoption: asyncio.TimerHandle | None = None
        # Store reads the UI awaits, in arrival order, until served or cancelled.
        self.pending: dict[int, ReadRequest] = {}
        # Owner reads being asked, until answered or cancelled.
        self.owner_reads: dict[int, asyncio.Future] = {}
        self.tasks = asyncio.TaskGroup()
        self.closed = False

    def retire_pending(self, request_id: int) -> None:
        self.pending.pop(request_id)

    def mark_busy(self) -> None:
        self.busy.set()

    def accept(self, request: ObservationRequest) -> None:
        self.dispatch_sync(request)

    @handles(ObserveSidebar)
    def handle_observe_sidebar(self, request: ObserveSidebar) -> None:
        self.interest.receive(SidebarReading(request), self)

    @handles(ObserveThreads)
    def handle_observe_threads(self, request: ObserveThreads) -> None:
        self.interest.receive(ThreadReading(request), self)

    @handles(ObserveTeamHud)
    def handle_observe_team_hud(self, request: ObserveTeamHud) -> None:
        self.interest.receive(TeamHudReading(request), self)

    @handles(ObserveViews)
    def handle_observe_views(self, request: ObserveViews) -> None:
        self.interest.receive(ViewReading(request), self)

    @handles(ObserveNotifications)
    def handle_observe_notifications(self, request: ObserveNotifications) -> None:
        self.interest.receive(NotificationReading(request), self)

    @handles(StopSidebar)
    def handle_stop_sidebar(self, request: StopSidebar) -> None:
        self.interest.remove(SidebarReading)

    @handles(StopTeamHud)
    def handle_stop_team_hud(self, request: StopTeamHud) -> None:
        self.interest.remove(TeamHudReading)

    @handles(StopObserving)
    def handle_stop_observing(self, request: StopObserving) -> None:
        self.interest = self.interest.stop()

    @handles(ReadRequest)
    def handle_read_request(self, request: ReadRequest) -> None:
        self.pending[request.request_id] = request
        self.changed.set()

    @handles(OwnerRead)
    def handle_owner_read(self, request: OwnerRead) -> None:
        self.owner_reads[request.request_id] = self.tasks.create_task(
            self.serve_owner_read(request),
        )

    @handles(CancelRead)
    def handle_cancel_read(self, request: CancelRead) -> None:
        self.pending.pop(request.request_id, None)
        if (asking := self.owner_reads.pop(request.request_id, None)) is not None:
            asking.cancel()

    async def serve_owner_read(self, request: OwnerRead) -> None:
        try:
            answer = await request.answer(self.comms)
        finally:
            self.owner_reads.pop(request.request_id, None)
        self.send(answer)

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
            self.accept(request)

    async def watch(self) -> None:
        paths = {
            path
            for group in WireRevision.source_paths(self.comms.views).values()
            for path in group
        }
        paths.update(NotificationAssignment.wake_paths(self.comms.root))
        sources = tuple(FileSource(path) for path in paths)
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

    observe_gap: ClassVar[float] = 0.25

    def adopt_revision(self) -> None:
        """Read the stores' revision once the previous one's readings have finished and the
        gap has passed; until then keep presenting the held revision."""
        if self.stepping or asyncio.get_running_loop().time() < self.settled_at + self.observe_gap:
            self.adoption_owed = True
            return
        self.adoption_owed, self.unsettled = False, True
        revision = self.comms.views.revision()
        if self.revision.changed_since(StoresView(), revision):
            self.send(RevisionObserved(revision))
        self.revision = revision

    def adopt_later(self) -> None:
        self.adoption = None
        self.observe_again()

    async def observe(self, attempted: set[int]) -> None:
        self.adopt_revision()
        await self.interest.observe(self, self.revision, attempted)
        self.stepping = self.observe_due
        if self.stepping:
            return
        loop = asyncio.get_running_loop()
        if self.unsettled:
            self.settled_at, self.unsettled = loop.time(), False
        if self.adoption_owed and self.adoption is None:
            self.adoption = loop.call_later(
                max(0.0, self.settled_at + self.observe_gap - loop.time()), self.adopt_later)

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
            self.observe_again()

    async def pass_once(self) -> None:
        """One wake: answer waiting reads, then observe once if a change is due."""
        self.changed.clear()
        attempted: set[int] = set()
        await self.answer_pending(attempted)
        if self.observe_due and not self.closed:
            self.observe_due = False
            await self.observe(attempted)

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
                    await self.pass_once()
            finally:
                self.retire()
                loop.remove_reader(self.connection.fileno())
                watching.cancel()
                expiring.cancel()
                retrying.cancel()
                self.pending.clear()
                self.comms.views.transcript_reads.close()


class ServiceEnd(EncodedConnection):
    """The observation service's end: results out, requests in."""

    sends = ServiceResult
    receives = ObservationRequest


OBSERVATION_SERVICE_PROCESS = ProcessName("ac-observation")
OBSERVATION_PRELOAD_MODULE = f"{__package__}.observation_preload"


def warm_observation_forkserver() -> None:
    """Load store declarations, build declared table schemas once, and freeze the heap."""
    LockedStore.load_declarations()
    for table in TypedTable.members_with(TypedTable):
        _ = table.schema_names
    started()


def serve_encoded(route: CommsRoute, service_socket: socket.socket) -> None:
    """Serve one remote viewer over its socket, decoding only declared records."""
    serve(route, ServiceEnd(service_socket.detach()))


def serve(route: CommsRoute, connection: Connection) -> None:
    """Observe the caller's selected route, including its reviewed native package."""
    OBSERVATION_SERVICE_PROCESS.publish()
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


@dataclass(frozen=True)
class LocalRouteObservation(ObservationSource):
    """A route observed in a local process on its root, started the way ``start`` says."""

    route: CommsRoute
    start: HelperStart = field(default_factory=SpawnedHelper)

    @classmethod
    def awaited(cls, route: CommsRoute) -> ObservationProcess:
        process = ObservationProcess(cls(route))
        process.start_receiving(process.observed, process.failed)
        return process

    def observation_peer(self, codec: type[EncodedConnection] = ViewerEnd) -> LocalObservationPeer:
        return LocalObservationPeer(self.route, self.start)


class LocalObservationPeer(ObservationPeer):
    """The service in a spawned process of this machine, over a pipe."""

    def __init__(self, route: CommsRoute, start: HelperStart) -> None:
        context = start.context()
        self.connection, child = context.Pipe()
        with ExitStack() as acquisition:
            acquisition.enter_context(self.connection)
            with child:
                self.process = context.Process(
                    target=serve, args=(route, child), daemon=False,
                    name="agent-comms-observation",
                )
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
