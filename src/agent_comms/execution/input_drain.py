"""Session input ownership: acceptance, delivery cursors, queues and wake draining."""

from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import aclosing, asynccontextmanager
from dataclasses import dataclass, field, replace
from functools import partial, reduce
from typing import ClassVar, cast

from acp.schema import PromptResponse

from agent_comms.acp_extension import (
    InputDeliveryChangedUpdate,
    LiveQueueScope,
    NoQueueScope,
    PromptRequest,
    QueueChangedUpdate,
    QueueScope,
    encode_updates,
)
from agent_comms.comms import Comms
from agent_comms.config_options import ConfigOptions
from agent_comms.coordination.cohort import next_sealed_assignment
from agent_comms.coordination.cohort_schema import AcceptanceFrontier
from agent_comms.coordination.coordinator import Coordination
from agent_comms.coordination.database import CoordinationStore
from agent_comms.coordination.participant_store import ParticipantSnapshot
from agent_comms.coordination.tables.assignments import WakeAssignment
from agent_comms.errors import RelationViolationError, UnregisteredThreadError
from agent_comms.execution.child_process import join_retirement
from agent_comms.execution.controller import SessionUpdateSink
from agent_comms.execution.cursor_owner import CursorOwner, CursorState
from agent_comms.execution.drain_failures import (
    DEFERRED_DRAIN_ERRORS,
    UNAVAILABLE_DRAIN_ERRORS,
    drain_failures,
    record_drain_failure,
)
from agent_comms.execution.following_input_contracts import FollowingInputRetirement
from agent_comms.execution.image_input import ImageInput
from agent_comms.execution.input_effects import InputEffects
from agent_comms.execution.input_event_source import InputEventSource, InputEventState
from agent_comms.execution.input_key import InputKey
from agent_comms.execution.native_input_owner import RegistryOwner
from agent_comms.execution.proven_source_coverage import SourcePrefix
from agent_comms.execution.queue_publication import QueuePublication
from agent_comms.execution.queued_input import DeliveredInput, InputHandoffRefused, QueuedInput
from agent_comms.execution.reservation_rules import (
    ReservationRule,
    ReservationViolationError,
    RuleCheck,
)
from agent_comms.execution.session_lifecycle import PublishedSession, SessionLifecycle
from agent_comms.execution.session_owner_reads import OwnedSessionLifetime, SessionTaskOwner
from agent_comms.execution.session_tasks import (
    BackgroundTask,
    RunningBackgroundTask,
    RunningTurnTask,
    TurnTask,
    WatchingObservationTask,
)
from agent_comms.execution.turn_context import TurnContext, UserFollowupSegment
from agent_comms.execution.turn_inbox import (
    ClearQueuedInputs,
    InputContent,
    SendNow,
    TurnInbox,
    TurnInboxItem,
)
from agent_comms.execution.turn_input_source import (
    AcceptedFollowingInput,
    NoTurnAcceptance,
    OriginalTurnInput,
)
from agent_comms.execution.turn_outcome import TurnOutcome
from agent_comms.execution.turn_terminal import OwnerStop
from agent_comms.input_attempt import (
    UNOBSERVED_DELIVERY_SCOPE,
    InputDeliveryScope,
    OwnerQueueInputDeliveryScope,
)
from agent_comms.input_disposition import InputDispositions, InputDocument
from agent_comms.messaging.bus import Bus
from agent_comms.messaging.routing import ScheduledTurn
from agent_comms.messaging.watched import ChangeSignal, FileLifetimeSource, FileSource
from agent_comms.nominal.declared_family import DeclaredFamily
from agent_comms.nominal.field_codec import AcpSessionId, InputId, RootId, ThreadLookup
from agent_comms.nominal.lifecycle_capability import Executing
from agent_comms.planning.goals import GoalDeclaration
from agent_comms.registry.activity import FailedWakeDiagnostic, StoppedDrainDiagnostic
from agent_comms.registry.registry_document import RegistryDocument, RegistryEntry
from agent_comms.registry.registry_store import RegistryEntryRevision
from agent_comms.registry.thread_identity import ClaimedIdentity, OwnerIdentity
from agent_comms.registry.threads import NativeThread, Thread
from agent_comms.storage.locked_store import GoalWakeFile, RegistryFile
from agent_comms.storage.maintenance_barrier import (
    MaintenanceIngress,
    MaintenancePhase,
    MaintenanceStatus,
)
from agent_comms.transport.owner_requests import OwnerRetiredReply, StopOwnerRuntimeRequest
from agent_comms.transport.runtime_server import RuntimeServer

AGENT_PREFIX = "!agent "


class WakeOutcome(DeclaredFamily, TurnOutcome):
    """A completed wake owns what its session hears of it."""

    @abstractmethod
    def settle(self, inputs: InputObservation, session_id: AcpSessionId) -> None: ...


@dataclass(frozen=True)
class WakeHeld(WakeOutcome):
    hold: MaintenanceStatus

    def settle(self, inputs: InputObservation, session_id: AcpSessionId) -> None:
        """Maintenance reopening supplies the next wake."""
        inputs.state(session_id).receive(WakeUnfinished())


class WakeDue(WakeOutcome):
    def settle(self, inputs: InputObservation, session_id: AcpSessionId) -> None:
        """The wake is over, so its observer looks at the inbox again: that drain starts the
        next wake for whatever this one could not take. The wake used to drain once more
        itself before ending, a request for the next wake made while it was still the
        pending one, which the schedule refused every time."""
        inputs.wake_observer(session_id)


class BackgroundWake(RuleCheck):
    """A check that starts work in the background, so configuration can disable it."""


@dataclass(frozen=True, kw_only=True)
class SessionScheduleCheck(RuleCheck):
    session_id: AcpSessionId
    inputs: InputDrain


class SchedulesWake(SessionScheduleCheck, ABC):
    """A check that schedules a wake, so it waits for the previous wake to finish."""

    def schedule(self) -> None:
        try:
            self.require_valid()
        except ReservationViolationError as refusal:
            refusal.rule.refusing(self)
            return
        self.inputs.register_wake(
            self.session_id, self.inputs.runtime.background(self.run())
        )

    @abstractmethod
    async def run(self) -> WakeOutcome: ...


class WakeScheduleCheck(SchedulesWake, BackgroundWake):
    """Check wake permission and execute it under the existing turn lock.

    A queued wake never carries a stale owner grant across a lock wait. The
    current owner and active goal are read only after acquiring the turn lock.
    """

    async def run(self) -> WakeOutcome:
        inputs, session_id = self.inputs, self.session_id
        while inputs.state(session_id).pending_turns:
            if ClosingScheduleRule().violated(self):
                return WakeDue()
            async with inputs.state(session_id).turn_lock:
                # The bus may accept a channel post while an operator drains the
                # fleet. Keep its existing queued origin; do not start a native
                # turn or consume it until this root's typed phase is READY.
                hold = await Coordination.run_worker(inputs.comms.owners.maintenance.read)
                if hold.turn_pending:
                    return WakeHeld(hold)
                snapshot = await Coordination.run_worker(inputs.comms.registry.snapshot)
                owner = snapshot.require(inputs.sessions.require(session_id))
                try:
                    snapshot.status(owner.name).require_running()
                except RelationViolationError:
                    inputs.state(session_id).pending_turns = []
                    continue
                state = inputs.state(session_id)
                pending, state.pending_turns = state.pending_turns, []
                goal = owner.goal.active()
                self._retain_goal_turns(pending, goal)
                if not pending:
                    continue
                pending, remaining = ScheduledTurn.take_batch(pending)
                if remaining:
                    state.pending_turns = remaining
                async with inputs.state(session_id).own_turn_task():
                    await inputs.effects.turns.run_agent_turn(
                        session_id,
                        inputs.sessions.require(session_id),
                        "\n\n".join(turn.prompt for turn in pending),
                        acceptance=NoTurnAcceptance(),
                        origins=tuple(origin for turn in pending for origin in turn.origin.messages()),
                        autonomous_goal=len(pending) == 1 and pending[0].autonomous_goal,
                    )
        return WakeDue()

    @staticmethod
    def _retain_goal_turns(pending: list[ScheduledTurn], goal: GoalDeclaration) -> None:
        """Discard stale goal work without dropping independent incoming turns."""
        active_ids = frozenset(current.id for current in goal)
        pending[:] = [
            turn for turn in pending
            if not turn.autonomous_goal or turn.goal_id.retained_by(active_ids)
        ]


class ClosingScheduleRule(ReservationRule):
    check_type = SessionScheduleCheck
    explanation = "Input draining is closing."

    def violated(self, check):
        return check.inputs.closing


class PendingWakeScheduleRule(ReservationRule):
    check_type = SchedulesWake
    explanation = "A previously scheduled wake has not finished."

    def violated(self, check):
        return check.inputs.state(check.session_id).wake_pending()

    def refusing(self, check):
        check.inputs.state(check.session_id).receive(BehindWake())


class DisabledWakeScheduleRule(ReservationRule):
    check_type = BackgroundWake
    explanation = "Background waking is disabled by the input/runtime configuration."

    def violated(self, check):
        return check.inputs.background_wakes_disabled


class EmptyWakeScheduleRule(ReservationRule):
    check_type = WakeScheduleCheck
    explanation = "No queued turn requires a background wake."

    def violated(self, check):
        return not check.inputs.state(check.session_id).pending_turns


class PrivateObservationState(ABC):
    """What the last settled drain saw of this owner's inbox, or that none has settled."""

    @abstractmethod
    def has_unobserved_inputs(self, bus: Bus, lookup: ThreadLookup) -> bool: ...


class NoPrivateObservation(PrivateObservationState):
    def has_unobserved_inputs(self, bus: Bus, lookup: ThreadLookup) -> bool:
        return True


@dataclass(frozen=True, slots=True)
class InboxWork:
    """What a drain reads beyond its message cut and registry entry. Two observations with
    the same work would drain the same way."""

    root_id: RootId
    auto_wake: bool
    published: PublishedSession | None
    turn_open: bool
    turn_running: bool
    participant: ParticipantSnapshot
    pending: WakeAssignment | None
    cursor: CursorState
    frontier: AcceptanceFrontier
    store_inode: tuple[int, int]
    maintenance_phase: type[MaintenancePhase]
    maintenance_generation: int


@dataclass(frozen=True, slots=True)
class PrivateObservation(PrivateObservationState):
    """One drain's observation: its message cut, registry entry and own coordination work."""

    source_prefix: SourcePrefix
    entry: RegistryEntry
    work: InboxWork

    def has_unobserved_inputs(self, bus: Bus, lookup: ThreadLookup) -> bool:
        """Every root commit wakes every session here; most commits append no message."""
        through = self.source_prefix.through_seq
        return (self.source_prefix.require_current(bus) > through
                and bus.received_after(lookup, through))

    async def drain(self, inputs, session_id: AcpSessionId, root_id: RootId) -> None:
        await inputs.effects._drain_private_nk(session_id, root_id)


class ExecutionWaitingObservation(PrivateObservation):
    """A previous execution blocks new input until its stored state changes."""

    async def drain(self, inputs, session_id: AcpSessionId, root_id: RootId) -> None:
        """Nothing to hand over until that execution's stored state changes."""


class InboxNews(ABC):
    """Something that happened which bears on whether a session owes its inbox a drain.

    Whoever sees it happen hands it to the session, and the session's debt answers it. The
    watcher and the turn used to leave a boolean each for seven separate tests to combine."""

    def reaching(self, debt: InboxDebt) -> InboxDebt:
        """The debt after this: by default, kept for a settled inbox to weigh at its next tick."""
        return debt.hearing(self)


class WeighedNews(InboxNews):
    """News a settled inbox keeps until a tick has weighed it."""

    asks_a_store: ClassVar[bool] = False

    @abstractmethod
    async def owes_tick(
        self, session: DrainedSession, note: PrivateObservationState, bus: Bus,
        lookup: ThreadLookup,
    ) -> bool:
        """Whether, having happened, this owes the settled session a tick now. A tick looks
        at the session's registry entry; it drains the inbox only when the debt itself owes
        one, so news that means mail says so to the debt."""


@dataclass(frozen=True, slots=True)
class BusCommitted(WeighedNews):
    """The bus committed. Every root commit reaches every session; the bus says whether this
    one appended a message for this owner past the cut its note was taken at."""

    asks_a_store = True

    async def owes_tick(
        self, session: DrainedSession, note: PrivateObservationState, bus: Bus,
        lookup: ThreadLookup,
    ) -> bool:
        # Weighed from here: a commit that lands while the bus is being asked is heard anew.
        session.inbox = session.inbox.dismissing(self)
        if await Coordination.run_worker(partial(note.has_unobserved_inputs, bus, lookup)):
            # Mail past the note's cut: the inbox owes a drain, whatever else it was doing.
            session.inbox = session.inbox.changed()
            return True
        return False


@dataclass(frozen=True, slots=True)
class RowMoved(WeighedNews):
    """This owner's registry entry differs from the one the root observer read before it, or
    could not be read. It is what the observer of a running wake still watches: that wake
    wakes the observer itself when it finishes. It owes the entry a look (its settings, its
    goals), and the inbox nothing: a wake moves its own row by starting its turn, and each
    one used to cost a whole drain that found nothing."""

    async def owes_tick(
        self, session: DrainedSession, note: PrivateObservationState, bus: Bus,
        lookup: ThreadLookup,
    ) -> bool:
        return session.wake_pending()


@dataclass(frozen=True, slots=True)
class LocalChange(InboxNews):
    """Something in this process moved that need not add a new input file: a turn or a wake
    ended, a session attached."""

    def reaching(self, debt: InboxDebt) -> InboxDebt:
        return debt.changed()


class WakeStarted(InboxNews):
    """A wake was registered for this session."""

    def reaching(self, debt: InboxDebt) -> InboxDebt:
        return debt.wake_started()


class BehindWake(InboxNews):
    """A wake was asked for while an earlier one had not finished, so none was registered.
    The earlier wake's end is news to this session."""

    def reaching(self, debt: InboxDebt) -> InboxDebt:
        return debt.behind_wake()


class NoteVoided(InboxNews):
    """Whatever note the session held no longer describes its inbox: a drain is owed."""

    def reaching(self, debt: InboxDebt) -> InboxDebt:
        return DrainOwed()


class PendingTaken(NoteVoided):
    """A running turn took accepted work from the inbox."""


class WakeUnfinished(NoteVoided):
    """A wake ended without doing its work: it failed, was cancelled, or maintenance held it.
    The inbox it was handed is as the drain left it, so the next tick drains again. Nothing
    wakes the observer for it: maintenance reopening and the next commit both tick, and
    waking now would spin on the same closed phase."""


@dataclass(frozen=True, slots=True)
class InboxUnchanged(InboxNews):
    """A wake that found nothing to run saw the inbox the same before and after its own
    cursor publication."""

    note: PrivateObservation

    def reaching(self, debt: InboxDebt) -> InboxDebt:
        return debt.witnessed(self.note)


class InboxDebt(ABC):
    """What a session owes its inbox. A session holds exactly one member.

    Five fields used to hold this between them (a note, a repeat flag, two bus flags and an
    entry flag), so a session could be settled and owing at once, and each of seven tests
    resolved that its own way."""

    quiet: ClassVar[bool] = False
    """Nothing is owed until news arrives."""
    asks_again: ClassVar[bool] = False
    """Owed whatever the stores say: any tick observes, and an observation that leaves it so
    asks for another."""

    @property
    def note(self) -> PrivateObservationState:
        return NoPrivateObservation()

    def holds(self, observation: PrivateObservation) -> bool:
        return self.note == observation

    def hearing(self, news: WeighedNews) -> InboxDebt:
        """A debt that drains at its next tick anyway has nothing to keep."""
        return self

    def dismissing(self, news: WeighedNews) -> InboxDebt:
        return self

    def changed(self) -> InboxDebt:
        return DrainOwed(self.note)

    def witnessed(self, note: PrivateObservation) -> InboxDebt:
        return self

    def confirmed(self) -> InboxDebt:
        """An observation found the inbox exactly as this debt's note describes it."""
        return self

    def wake_started(self) -> InboxDebt:
        return self

    def behind_wake(self) -> InboxDebt:
        return self

    def settled(self, note: PrivateObservation) -> InboxDebt:
        """The drain begun at ``note`` left nothing more for this session to do there."""
        return self

    async def conclude(
        self, inputs: InputDrain, session_id: AcpSessionId, root_id: RootId,
        note: PrivateObservation,
    ) -> None:
        """The drain begun at ``note`` returned under this debt. One told mid-drain that its
        note is void still owes a drain, and has nothing to conclude."""

    def abandoned(self) -> InboxDebt:
        """The drain begun under this debt ended without settling."""
        return self

    async def owed(
        self, session: DrainedSession, needs_refresh: bool, bus: Bus, lookup: ThreadLookup,
    ) -> bool:
        return True

    async def drain(self, inputs: InputDrain, session_id: AcpSessionId) -> None:
        """A tick's share of this debt."""
        await inputs.drain_inbox(session_id)


@dataclass(frozen=True, slots=True)
class DrainOwed(InboxDebt):
    """A drain is owed. ``held`` is what the last settled one saw: an observation that finds
    exactly that again settles without draining."""

    held: PrivateObservationState = field(default_factory=NoPrivateObservation)
    asks_again = True

    @property
    def note(self) -> PrivateObservationState:
        return self.held

    def changed(self) -> InboxDebt:
        return self

    def witnessed(self, note: PrivateObservation) -> InboxDebt:
        return DrainOwed(note)

    def confirmed(self) -> InboxDebt:
        return InboxSettled(self.held)


@dataclass(frozen=True, slots=True)
class InboxSettled(InboxDebt):
    """A drain left nothing owed at ``held``. Only news can change that; what has been heard
    and not yet weighed is ``unweighed``. A new note has never asked the bus."""

    held: PrivateObservationState
    unweighed: frozenset[WeighedNews] = frozenset({BusCommitted()})
    quiet = True

    @property
    def note(self) -> PrivateObservationState:
        return self.held

    def hearing(self, news: WeighedNews) -> InboxDebt:
        return replace(self, unweighed=self.unweighed | {news})

    def dismissing(self, news: WeighedNews) -> InboxDebt:
        return replace(self, unweighed=self.unweighed - {news})

    def witnessed(self, note: PrivateObservation) -> InboxDebt:
        return InboxSettled(note, self.unweighed | {BusCommitted()})

    async def owed(
        self, session: DrainedSession, needs_refresh: bool, bus: Bus, lookup: ThreadLookup,
    ) -> bool:
        """Queued turns and a stale cursor are consumed by a drain, so they owe one;
        otherwise only news it has yet to weigh can owe a tick, asked without a store read
        first."""
        if session.pending_turns or needs_refresh:
            session.inbox = self.changed()
            return True
        for news in sorted(self.unweighed, key=lambda heard: heard.asks_a_store):
            if await news.owes_tick(session, self.held, bus, lookup):
                return True
        return False

    async def drain(self, inputs: InputDrain, session_id: AcpSessionId) -> None:
        """Nothing: a settled inbox drains when news makes it owe one, and then it is no
        longer this member."""


@dataclass(frozen=True, slots=True)
class InboxDraining(InboxDebt):
    """A drain is running, and has asked for no wake. What it hears reaches the debt it ends
    on, a local change included: heard a moment after the drain or in the middle of it, that
    leaves the note standing and a drain owed."""

    heard: frozenset[InboxNews] = frozenset()

    def hearing(self, news: InboxNews) -> InboxDebt:
        return replace(self, heard=self.heard | {news})

    def changed(self) -> InboxDebt:
        return self.hearing(LocalChange())

    def wake_started(self) -> InboxDebt:
        return WakeTookInbox(self.heard)

    def behind_wake(self) -> InboxDebt:
        return DrainBehindWake(self.heard)

    def resting(self, note: PrivateObservation) -> InboxDebt:
        """What this drain leaves the session owing at ``note``, before what it heard."""
        return InboxSettled(note)

    def settled(self, note: PrivateObservation) -> InboxDebt:
        return reduce(lambda debt, news: news.reaching(debt), self.heard, self.resting(note))

    async def conclude(
        self, inputs: InputDrain, session_id: AcpSessionId, root_id: RootId,
        note: PrivateObservation,
    ) -> None:
        """It found nothing to start, which holds only if nothing moved while it read: never
        absorb a message, owner or recovery change during a suspended read, or skip queued
        independent work after a completed native turn. So it looks once more."""
        if note != await inputs._observe_private_revision(session_id, root_id):
            return
        state = inputs.state(session_id)
        state.inbox = state.inbox.settled(note)
        await inputs.rest(session_id)

    def abandoned(self) -> InboxDebt:
        return DrainOwed()


@dataclass(frozen=True, slots=True)
class DrainAskedWake(InboxDraining):
    """The running drain found work and asked for a wake. A wake's end is news to this
    session however it ends, so the drain settles at the cut it drained from without looking
    again: the wake is already moving what a second look would compare."""

    async def conclude(
        self, inputs: InputDrain, session_id: AcpSessionId, root_id: RootId,
        note: PrivateObservation,
    ) -> None:
        inputs.state(session_id).inbox = self.settled(note)


@dataclass(frozen=True, slots=True)
class WakeTookInbox(DrainAskedWake):
    """The running drain registered a wake. That wake empties this inbox; until it ends, the
    one thing worth another drain is mail past the cut this one drained from. Without this
    member the drain's work left no note at all, and each commit of the wake it had just
    started ran a whole drain to look."""

    def behind_wake(self) -> InboxDebt:
        """A later request stands behind this drain's own wake."""
        return self


@dataclass(frozen=True, slots=True)
class DrainBehindWake(DrainAskedWake):
    """The running drain asked for a wake while an earlier one had not finished."""

    def resting(self, note: PrivateObservation) -> InboxDebt:
        return InboxAwaitsWake(note)


@dataclass(frozen=True, slots=True)
class InboxAwaitsWake(InboxSettled):
    """Work is pending behind a wake this session could not join. That wake's end is news,
    and it owes a drain whatever the inbox then looks like: the note here describes work
    still to do, so finding it again settles nothing. The drain used to say this by returning
    1, which left no note, and every tick until that wake ended drained to be refused again."""

    def changed(self) -> InboxDebt:
        return DrainOwed()

    def witnessed(self, note: PrivateObservation) -> InboxDebt:
        return self


type RegistryRead = asyncio.Task[RegistryDocument]


class RegistryObservation(ABC):
    """The registry as the root observer last read it.

    The registry announces each commit through its change file, which the root watches. A
    wake that does not carry that file found the registry as the last read left it, so that
    read still answers. The root woke 110 times for one channel message and read the
    registry on every one of them."""

    def following(
        self, names: tuple[str, ...], committed: bool, read: Callable[[tuple[str, ...]], RegistryRead],
    ) -> ObservedRegistry:
        """The read that answers this wake for ``names``: a new one unless a member keeps its own."""
        return ObservedRegistry(names, read(names))


class UnreadRegistry(RegistryObservation):
    """Nothing read yet, or a read that failed and answers for nothing."""


@dataclass(frozen=True)
class ObservedRegistry(RegistryObservation):
    names: tuple[str, ...]
    """The sessions whose entries this read decoded off the loop."""
    read: RegistryRead

    def following(
        self, names: tuple[str, ...], committed: bool, read: Callable[[tuple[str, ...]], RegistryRead],
    ) -> ObservedRegistry:
        if committed or names != self.names:
            return super().following(names, committed, read)
        return self


class BusSessionsRuntime:
    """The bus, its sessions and the runtime an owner reads."""

    comms: Comms
    sessions: SessionLifecycle
    runtime: RuntimeServer


class InputObservation(BusSessionsRuntime, ABC):
    """File-driven observation and process-local wake behavior."""

    effects: InputEffects
    auto_wake: bool
    closing: bool
    drained: dict[AcpSessionId, DrainedSession]
    root_observer: BackgroundTask
    observed_registry: asyncio.Task[RegistryDocument]

    @abstractmethod
    def state(self, session_id: AcpSessionId) -> DrainedSession: ...

    def turn_live(self, session_id: AcpSessionId) -> bool:
        state = self.state(session_id)
        return state.turn_open() or state.turn_task.busy

    def _retire_observer(self, session_id: AcpSessionId, state: DrainedSession,
                         task: asyncio.Task[None]) -> None:
        """Any death but a cancel is recorded as a stopped drain, so the board sees a deaf
        owner, not an idle one. The task's owner reads what ended it; the loop itself names no
        error it does not expect (plans/REMAINING-SLOP-PLAN.md, broad except)."""
        if state.observer.owns(task):
            state.observation_watcher = None
        if not task.cancelled() and (error := task.exception()) is not None:
            self.runtime.background(self._record_stopped(session_id, error))

    async def _record_stopped(self, session_id: AcpSessionId, error: BaseException) -> None:
        thread = self.sessions.require(session_id)
        snapshot = await Coordination.run_worker(self.comms.registry.snapshot)
        await record_drain_failure(
            self.comms, thread, snapshot.owner_identity(thread), StoppedDrainDiagnostic, error,
        )

    def wake_observer(self, session_id: AcpSessionId) -> None:
        self.ensure_live_drain(session_id)
        self.state(session_id).wake_observer()

    def register_wake(self, session_id: AcpSessionId, task: asyncio.Task[WakeOutcome]) -> None:
        state = self.state(session_id)
        state.wake_task = task
        state.receive(WakeStarted())

        def finished(completed: asyncio.Task[WakeOutcome]) -> None:
            if state.wake_task is not completed:
                return
            state.wake_task = None
            if completed.cancelled() or completed.exception() is not None:
                state.receive(WakeUnfinished())
                return
            completed.result().settle(self, session_id)

        task.add_done_callback(finished)

    def ensure_live_drain(self, session_id: AcpSessionId) -> None:
        """Share root observation, retaining an independent consumer for each session."""
        if self.closing:
            return
        if not self.root_observer.busy:
            self.root_observer = RunningBackgroundTask(self.runtime.background(self.observe_root()))
        state = self.state(session_id)
        if state.observing():
            return

        task = self.runtime.background(self.observe(session_id))
        state.observer = RunningBackgroundTask(task)
        task.add_done_callback(partial(self._retire_observer, session_id, state))
        state.wake_observer()
        self.root_observer.wake()

    async def _own_entry(self, session_id: AcpSessionId) -> RegistryEntryRevision:
        return await Coordination.run_worker(partial(
            self.comms.registry.entry, self.sessions.require(session_id),
        ))

    def _quiescent(self, session_id: AcpSessionId) -> bool:
        """No process-local work or retry is waiting on the next tick."""
        state = self.state(session_id)
        turn_live = self.turn_live(session_id)
        return (
            state.inbox.quiet
            and not state.pending_turns
            and not self.effects.cursors.delivery(session_id).needs_refresh
            and not state.wake_pending()
            and not turn_live
        )

    async def begin_observation(
        self, state: DrainedSession, entry: RegistryEntry,
    ) -> RegistryEntry:
        return entry

    def _read_observed_registry(self, names: tuple[str, ...]) -> RegistryDocument:
        document = self.comms.registry.store.read()
        for name in names:
            document.entry_if_present(name)
        return document

    def _reading_registry(self, names: tuple[str, ...]) -> RegistryRead:
        return asyncio.create_task(Coordination.run_worker(partial(
            self._read_observed_registry, names,
        )))

    async def observe_root(self) -> None:
        """One kernel registration and captured registry read feed the root's drains."""
        bus = self.comms.bus.bus
        inbox_sources = (
            *(FileSource(path) for path in (
                *bus.source_paths(self.comms.root), *CoordinationStore.source_paths(self.comms.root),
            )),
            FileLifetimeSource(bus.path),
            FileLifetimeSource(CoordinationStore.path_in(self.comms.root)),
        )
        bus_paths = frozenset((*bus.source_paths(self.comms.root), bus.path))
        registry_paths = frozenset(RegistryFile.source_paths(self.comms.root))
        observed: RegistryObservation = UnreadRegistry()
        sources = (
            *inbox_sources,
            *(FileSource(path) for path in GoalWakeFile.watched_paths_in(self.comms.root)),
        )
        entries: dict[AcpSessionId, RegistryEntry] = {}
        try:
            async with aclosing(bus.watch_sources(*sources)) as observations:
                async for watcher in observations:
                    self.root_observer = WatchingObservationTask(
                        cast(asyncio.Task[object], asyncio.current_task()), watcher,
                    )
                    targets = {session_id: state for session_id, state in self.drained.items()
                               if state.observing()}
                    names = tuple(self.sessions.require(session_id) for session_id in targets)
                    observed = current = observed.following(
                        names, not registry_paths.isdisjoint(watcher.observed_paths),
                        self._reading_registry,
                    )
                    read = current.read
                    try:
                        document = await read
                    except UNAVAILABLE_DRAIN_ERRORS:
                        # Each consumer records failure against its own original owner.
                        observed = UnreadRegistry()
                        entries = {}
                        for state in targets.values():
                            state.receive(RowMoved())
                            state.observation_ready.set()
                    else:
                        self.effects.turns.goals.observe_root(
                            document, (owner for state in targets.values()
                                       for owner in state.executors()), watcher.observed_paths,
                        )
                        # An owner's own turn claims itself and is seen again in its entry;
                        # its end reaches the session as a local change.
                        read_entries = {
                            session_id: entry.apart_from_turn()
                            for session_id, name in zip(targets, names, strict=True)
                            for entry in (document.entry_if_present(name),)
                            if entry is not None
                        }
                        for session_id, state in targets.items():
                            if entries.get(session_id) != read_entries.get(session_id):
                                state.receive(RowMoved())
                        entries = read_entries
                    self.observed_registry = read
                    inbox_changed = any(source.path in watcher.observed_paths for source in inbox_sources)
                    if not bus_paths.isdisjoint(watcher.observed_paths):
                        for state in self.drained.values():
                            state.receive(BusCommitted())
                    for state in targets.values():
                        if state.observing():
                            # Pending producers have their own completion wake;
                            # only an explicit local request owes another tick.
                            if inbox_changed or state.inbox.asks_again:
                                state.observation_ready.set()
                            state.observation_watcher = watcher
        finally:
            self.root_observer = BackgroundTask()
            # A failed native registration cannot leave consumers waiting forever.
            # Session retirement still owns turns, grants and queued input.
            for state in tuple(self.drained.values()):
                state.observer.cancel()

    async def observe(self, session_id: AcpSessionId) -> None:
        """Keep this session's input loop alive; observe_root reaches only a live one.

        A contended read restarts recovery at the next real change. Any other death ends the
        task, and `_retire_observer` records it as a stopped drain.
        """
        state = self.state(session_id)
        while True:
            try:
                await self._observe_inbox(session_id, state)
            except DEFERRED_DRAIN_ERRORS:
                await state.observation_ready.wait()
                state.observation_ready.clear()

    async def _observe_inbox(self, session_id: AcpSessionId, state: DrainedSession) -> None:
        initial = await self._own_entry(session_id)
        entry = await self.begin_observation(state, initial.entry)
        bus = self.comms.bus.bus
        lookup = entry.thread.incarnation.require_recorded().lookup
        state.inbox = DrainOwed()
        while True:
            await state.observation_ready.wait()
            state.observation_ready.clear()
            read = self.observed_registry
            if not await state.inbox.owed(
                state, self.effects.cursors.delivery(session_id).needs_refresh, bus, lookup,
            ):
                continue
            try:
                document = await asyncio.shield(read)
                entry = document.entry(self.sessions.require(session_id))
            except UNAVAILABLE_DRAIN_ERRORS as error:
                thread = self.sessions.require(session_id)
                snapshot = await Coordination.run_worker(self.comms.registry.snapshot)
                await record_drain_failure(
                    self.comms, thread, snapshot.owner_identity(thread),
                    FailedWakeDiagnostic, error,
                )
            else:
                # This observation reads the entry the root observer just read.
                state.inbox = state.inbox.dismissing(RowMoved())
                await self._observe_entry(session_id, entry)

    async def _observe_entry(
        self, session_id: AcpSessionId, entry: RegistryEntry,
    ) -> None:
        thread = self.sessions.require(session_id)
        try:
            async with drain_failures(self.comms, thread, entry.owner_identity):
                attachment = self.state(session_id).owned().identifier
                await self.sessions.config.apply_stored_settings(attachment)
                await self.state(session_id).inbox.drain(self, session_id)
                if self.effects.turns.goals.in_standby(attachment):
                    await Coordination.run_worker(partial(
                        self.comms.goals.recover_replied_goal_wait, session_id,
                    ))
                    if self.effects.turns.goals.in_standby(attachment):
                        await Coordination.run_worker(partial(
                            self.comms.goals.recover_closed_goal_wait, session_id,
                        ))
                await self.effects.turns.project_resume_check(session_id, self).schedule()
                await self.effects.turns.goals.schedule_goal(attachment)
                current = await Coordination.run_worker(partial(self.comms.registry.require, thread))
                if (current.turn_record.items_with(Executing, current) or self.turn_live(session_id)
                        or self._quiescent(session_id)):
                    return
                state = self.state(session_id)
                if state.self_tick_owed(
                    self.effects.cursors.delivery(session_id).needs_refresh,
                    self.turn_live(session_id),
                ):
                    state.notify_observer()
        except DEFERRED_DRAIN_ERRORS:
            pass

    def _private_observation_revision(
        self, session_id: AcpSessionId, root_id: RootId, source_prefix: SourcePrefix, entry: RegistryEntry,
        store: Coordination,
    ) -> PrivateObservation:
        """Original owner/work observations, never delivery or replay authority.

        Other owners' registry phases and SQL writes cannot change this owner's
        observation. Committed wire messages, own pending/recovered work and
        physical replacement still can. Silent context records consume bytes,
        not the message cut used to select delivery.
        """
        owner = RegistryOwner(
            thread=NativeThread.stored(entry.thread), claimed_generation=entry.claimed_generation)
        with store.session.read():
            participant = store.participants.get(owner.thread.incarnation.require_recorded().lookup)
            cursor = CursorOwner(
                thread=owner.thread, claimed_generation=owner.claimed_generation,
                wire_root_id=root_id, generation=participant.owner_identity.generation,
            ).state(store.session._connection)
            pending = next_sealed_assignment(
                store, participant.lookup, after_seq=cursor.covered_seq,
            )
            frontier = AcceptanceFrontier.require(
                store.session._connection, wire_root_id=root_id,
                recipient_lookup=participant.lookup,
            )
            original = store.session.path.stat()
        observation = PrivateObservation
        if (
            participant.pointer.unresolved
            and not self.owns_execution(session_id, owner.thread)
        ):
            observation = ExecutionWaitingObservation
        maintenance = self.comms.owners.maintenance._current_unlocked()
        return observation(source_prefix, entry, InboxWork(
            root_id=root_id,
            auto_wake=self.auto_wake,
            published=self.sessions.published.get(session_id),
            turn_open=self.state(session_id).turn_open(),
            turn_running=self.state(session_id).turn_task.busy,
            participant=participant,
            pending=pending,
            cursor=cursor,
            frontier=frontier,
            store_inode=(original.st_dev, original.st_ino),
            maintenance_phase=maintenance.phase,
            maintenance_generation=maintenance.generation,
        ))

    async def _observe_private_revision(
        self, session_id: AcpSessionId, root_id: RootId
    ) -> PrivateObservation:
        # Pending acquisition is not an inbox failure or a completed read.
        source_prefix = await Coordination.run_worker(partial(
            SourcePrefix.capture, self.comms.bus.bus, 0))
        entry = (await self._own_entry(session_id)).entry
        # Release all bus locks before starting registry/SQL consumer work.
        # The original coordinator owns its bounded read acquisition policy;
        # the observation must not replace it with a zero-wait failure.
        return await Coordination.run_read_async(
            CoordinationStore.path_in(self.comms.root),
            partial(self._private_observation_revision, session_id, root_id, source_prefix, entry),
        )


class DrainTurn(ABC):
    """The drained turn owns its inbox lifetime and captured original together."""

    def is_open(self) -> bool:
        return False

    def has_source(self) -> bool:
        return False
    @property
    def keys(self) -> tuple[str, ...]:
        return ()

    def for_owner(self, owner: Thread) -> DrainTurn:
        return self if owner.accepts_following_input else NoDrainTurn()

    @abstractmethod
    def opened(self) -> DrainTurn: ...

    def capture(self, source: OriginalTurnInput) -> DrainTurn:
        raise KeyError("Session has no running turn inbox")

    def matches(self, inbox: TurnInbox) -> bool:
        return False

    def require_inbox(self) -> TurnInbox:
        raise KeyError("Session has no running turn inbox")

    def require_source(self) -> OriginalTurnInput:
        raise KeyError("Session has no started first input")

    @abstractmethod
    def send(self, item: TurnInboxItem) -> None: ...

    @abstractmethod
    async def resume(self, maintenance: MaintenanceIngress) -> None: ...

    @abstractmethod
    async def finish_original(self, inputs: InputDrain) -> None: ...


class NoDrainTurn(DrainTurn):
    async def finish_original(self, inputs: InputDrain) -> None:
        pass

    def opened(self) -> DrainTurn:
        return AwaitingDrainTurn(TurnInbox())

    def send(self, item: TurnInboxItem) -> None:
        pass

    async def resume(self, maintenance: MaintenanceIngress) -> None:
        pass


@dataclass(frozen=True)
class AwaitingDrainTurn(DrainTurn):
    inbox: TurnInbox

    def is_open(self) -> bool:
        return True

    async def finish_original(self, inputs: InputDrain) -> None:
        pass

    def opened(self) -> DrainTurn:
        return self

    def capture(self, source: OriginalTurnInput) -> DrainTurn:
        return StartedDrainTurn(self.inbox, source)

    def matches(self, inbox: TurnInbox) -> bool:
        return self.inbox is inbox

    def require_inbox(self) -> TurnInbox:
        return self.inbox

    def send(self, item: TurnInboxItem) -> None:
        self.inbox.put_nowait(item)

    async def resume(self, maintenance: MaintenanceIngress) -> None:
        async with maintenance.ingress_async() as status:
            with status.phase.send_running(None) as outcome:
                outcome.resume(self.inbox)


@dataclass(frozen=True)
class StartedDrainTurn(AwaitingDrainTurn):
    source: OriginalTurnInput

    def has_source(self) -> bool:
        return True

    @property
    def keys(self) -> tuple[str, ...]:
        return self.source.keys

    def require_source(self) -> OriginalTurnInput:
        return self.source

    async def finish_original(self, inputs: InputDrain) -> None:
        await Coordination.run_worker(partial(inputs.finish_original_inputs, self.source.keys))


@dataclass(eq=False)
class DrainedSession(SessionTaskOwner, InputEventState):
    """One drained session's state, from its first use until the session ends."""

    identifier: AcpSessionId = field(kw_only=True)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    turn_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    turn_task: TurnTask = field(default_factory=TurnTask)
    observer: BackgroundTask = field(default_factory=BackgroundTask)
    wake_task: asyncio.Task[WakeOutcome] | None = None
    observation_watcher: ChangeSignal | None = None
    observation_ready: asyncio.Event = field(default_factory=asyncio.Event)
    turn: DrainTurn = field(default_factory=NoDrainTurn)
    queued: dict[InputId, QueuedInput] = field(default_factory=dict)
    restored: dict[InputId, QueuedInput] = field(default_factory=dict)
    following: dict[InputId, AcceptedFollowingInput] = field(default_factory=dict)
    queue_revision: int = 0
    pending_turns: list[ScheduledTurn] = field(default_factory=list)
    # What this session owes its inbox. It never authorizes delivery, acceptance or a
    # native send.
    inbox: InboxDebt = field(default_factory=DrainOwed)

    def owned(self) -> OwnerSession:
        raise RelationViolationError("This session has not acquired local execution")

    async def retire_for_stop(
        self, sessions: SessionLifecycle, request: StopOwnerRuntimeRequest,
    ) -> OwnerRetiredReply:
        """This host holds no session for the fenced owner: nothing is left to join, so
        completing it releases the row's binding to this host."""
        await sessions.complete_unheld(request.owner, request.claim)
        sessions.effects.inputs.end_session(self.identifier)
        return OwnerRetiredReply(request.owner)

    def configuration_for(self, ingress: ConfigOptions) -> ConfigOptions:
        return ingress

    def executors(self) -> tuple[OwnerSession, ...]:
        return ()

    def request_stop(self) -> None:
        """An attachment observes execution; it has no owner to stop."""

    @asynccontextmanager
    async def own_turn_task(self) -> AsyncIterator[None]:
        """Hold execution through cleanup; shutdown retirement suppresses the wake."""
        # Every entry is awaited by an asyncio task; bind its foreign handle here.
        running = RunningTurnTask(cast(asyncio.Task[object], asyncio.current_task()))
        self.turn_task = running
        try:
            yield
        finally:
            if self.turn_task is running:
                self.turn_task = TurnTask()
                self.wake_observer()

    def retire_turn_task(self) -> TurnTask:
        previous, self.turn_task = self.turn_task, TurnTask()
        return previous

    def receive(self, news: InboxNews) -> None:
        self.inbox = news.reaching(self.inbox)

    def wake_observer(self) -> None:
        self.receive(LocalChange())
        self.notify_observer()

    def self_tick_owed(self, needs_refresh: bool, turn_live: bool) -> bool:
        """Notify only when this observer can consume the work on its next tick.

        A scheduled turn and a pending wake have their own completion, which
        wakes the observer; notifying for them now would spin on the same work.
        Cursor refresh still needs its own tick.
        """
        if needs_refresh:
            return True
        if self.pending_turns or turn_live:
            return False
        return self.inbox.asks_again

    def producers(self) -> tuple[asyncio.Task[object], ...]:
        return (*self.observer.producers(),
                *(task for task in (self.wake_task,) if task is not None))

    def retire_producers(self) -> None:
        self.observer = BackgroundTask()
        self.wake_task = self.observation_watcher = None
        self.observation_ready.clear()
        self.inbox = DrainOwed()

    def turn_open(self) -> bool:
        return self.turn.is_open()

    def observing(self) -> bool:
        return self.observer.busy

    def wake_pending(self) -> bool:
        return self.wake_task is not None and not self.wake_task.done()

    def notify_observer(self) -> None:
        if self.observation_watcher is not None:
            self.observation_watcher.wake()

    def open_inbox(self) -> TurnInbox:
        self.turn = self.turn.opened()
        return self.turn.require_inbox()

    def capture_first_source(self, source: OriginalTurnInput) -> None:
        self.turn = self.turn.capture(source)

    def retire_turn(self, inbox: TurnInbox) -> DrainTurn:
        if not self.turn.matches(inbox):
            return NoDrainTurn()
        previous, self.turn = self.turn, NoDrainTurn()
        return previous

    def to_turn(self, item: TurnInboxItem) -> None:
        """A queue change reaches an open turn; without one the queue keeps it."""
        self.turn.send(item)

    def require_inbox(self) -> TurnInbox:
        return self.turn.require_inbox()

    def require_first_source(self) -> OriginalTurnInput:
        return self.turn.require_source()


@dataclass(eq=False)
class OwnerSession(DrainedSession, OwnedSessionLifetime):
    """Local execution retains its original configuration, never copies its fields."""

    configuration: ConfigOptions = field(kw_only=True)
    owner: OwnerIdentity = field(kw_only=True)
    stop: OwnerStop = field(kw_only=True)
    auth_observer: asyncio.Task[None] = field(kw_only=True)
    retirement_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    retired: asyncio.Future[None] = field(default_factory=asyncio.Future)
    _observations: set[asyncio.Future[None]] = field(default_factory=set, init=False, repr=False)

    def owned(self) -> OwnerSession:
        return self

    async def retire_for_stop(
        self, sessions: SessionLifecycle, request: StopOwnerRuntimeRequest,
    ) -> OwnerRetiredReply:
        return await request.retire(sessions, self)

    def configuration_for(self, ingress: ConfigOptions) -> ConfigOptions:
        return self.configuration

    def executors(self) -> tuple[OwnerSession, ...]:
        return (self,)

    async def observe_live(self, observe: Callable[[], Awaitable[object]]) -> None:
        """Join an observation this owner accepted before retirement drops session state.

        A shared source may retain this owner across a storage await. Whether it is
        accepted belongs to this owner's stop, not a later session-ID lookup that
        could create fresh unowned input state after retirement.
        """
        if self.stop.done():
            return
        completion = asyncio.get_running_loop().create_future()
        self._observations.add(completion)
        try:
            await observe()
        finally:
            completion.set_result(None)
            self._observations.remove(completion)

    async def join_observations(self) -> None:
        """Join accepted readers once stop refuses new ones; readers stay parallel."""
        await join_retirement(asyncio.gather(*self._observations))

    def request_stop(self) -> None:
        self.stop.request()

    def producers(self) -> tuple[asyncio.Task[object], ...]:
        return (*super().producers(), self.auth_observer)


class InputDrain(InputObservation, QueuePublication, InputEventSource):
    async def begin_observation(
        self, state: DrainedSession, entry: RegistryEntry,
    ) -> RegistryEntry:
        # A restarted observer may inherit a reservation whose live rollback
        # died with its owner. Live delivery failures restore their exact
        # reservation through DeliveredInput._restore_unplaced.
        async with drain_failures(self.comms, entry.thread.name, entry.owner_identity):
            await DeliveredInput.recover_interrupted(
                self, state, entry, self.comms.owners.require_private_launch().wire_root_id,
            )
        # Recovery suspends; derive the observer's identity from the current row.
        return (await Coordination.run_worker(partial(
            self.comms.registry.entry, entry.thread.name,
        ))).entry

    def owns_execution(self, session_id: AcpSessionId, owner: Thread) -> bool:
        """An attached turn can take follow-ups; a predecessor cannot."""
        return (
            owner.turn_record.read().phase.busy
            or self.turn_live(session_id)
        )

    def __init__(
        self,
        comms: Comms,
        sessions: SessionLifecycle,
        runtime: RuntimeServer,
        effects: InputEffects,
        auto_wake: bool,
        configuration: ConfigOptions,
    ):
        self.comms = comms
        self.sessions = sessions
        self.runtime = runtime
        self.effects = effects
        self.configuration = configuration
        self.drained: dict[AcpSessionId, DrainedSession] = {}
        self.root_observer: BackgroundTask = BackgroundTask()
        self.queue_lock = asyncio.Lock()
        self.dispositions = InputDispositions(comms.root)
        self.auto_wake = auto_wake
        self.closing = False

    def state(self, session_id: AcpSessionId) -> DrainedSession:
        """This session's state, created on its first use."""
        state = self.drained.get(session_id)
        if state is None:
            state = self.drained[session_id] = DrainedSession(identifier=session_id)
        return state

    def acquire_owner(
        self, session_id: AcpSessionId, configuration: ConfigOptions, owner: OwnerIdentity,
    ) -> OwnerSession:
        """Retain this allocated owner's original launch, never another host session's settings."""
        if session_id not in self.drained:
            self.drained[session_id] = OwnerSession(
                identifier=session_id,
                configuration=configuration,
                owner=owner,
                stop=OwnerStop(self.comms.owners.maintenance),
                auth_observer=self.runtime.background(configuration.observe_authentication(
                    session_id, self.sessions.config.publish_configuration,
                )),
            )
        owned = self.drained[session_id].owned()
        if owned.owner != owner:
            raise RelationViolationError("Another allocation already owns this session")
        return owned

    def end_session(self, session_id: AcpSessionId) -> None:
        """Ending a session drops its state, its live producers with it."""
        state = self.drained.pop(session_id, None)
        if state is not None:
            for task in state.producers():
                task.cancel()

    def end_sessions(self) -> None:
        for session_id in tuple(self.drained):
            self.end_session(session_id)

    async def clear_queued_inputs(self, session_id: AcpSessionId) -> None:
        """Drop prompts queued against the backend for this session.

        Pi's own queue is cleared, then the local deferred-display list is
        reset. Callers may re-send the prompts they want to keep.
        """
        async with self.queue_lock:
            retained = {}
            cleared = []
            state = self.state(session_id)
            for key, item in state.queued.items():
                remaining = item.after_clear()
                if remaining is None:
                    cleared.append(item)
                else:
                    retained[key] = remaining
            if cleared:
                await Coordination.run_worker(partial(
                    self.dispositions.clear_unbound, tuple(item.key for item in cleared),
                ))
            state.queued = retained
            if cleared:
                state.to_turn(ClearQueuedInputs(tuple(item.input_id for item in cleared)))
            state.restored = {}
        await self.emit_queue_state(session_id)

    async def queue_binding(self, session_id: AcpSessionId) -> QueueScope:
        try:
            owner, claim_scope = await Coordination.run_worker(partial(
                self.comms.registry.live_owner_with_claim,
                self.sessions.require(session_id) if session_id in self.sessions.published else session_id,
            ))
        except (RelationViolationError, UnregisteredThreadError):
            # The unavailable observation carries no local queue authority.
            return NoQueueScope()
        return LiveQueueScope(
            session_id=session_id,
            claimed_identity=ClaimedIdentity(owner.incarnation, claim_scope),
            owner=owner.require_process(),
        )

    def next_revision(self, session_id: AcpSessionId) -> int:
        state = self.state(session_id)
        state.queue_revision += 1
        return state.queue_revision

    async def queue_state(self, session_id: AcpSessionId) -> QueueChangedUpdate:
        """The producer owns the complete exact-ID queue projection."""
        scope = await self.queue_binding(session_id)
        state = self.state(session_id)
        return QueueChangedUpdate(scope, self.next_revision(session_id), scope.projection(
            tuple(state.queued.values()), tuple(state.restored.values()),
        ))

    async def emit_queue_state(
        self, session_id: AcpSessionId, *, client: SessionUpdateSink | None = None,
    ) -> None:
        await (client or self.runtime).publish_update(
            session_id=session_id,
            update=(await self.queue_state(session_id)).acp_chunk(),
        )

    async def emit_input_delivery_changed(
        self, session_id: AcpSessionId, *,
        client: SessionUpdateSink | None = None,
    ) -> None:
        await (client or self.runtime).publish_update(
            session_id=session_id,
            update=InputDeliveryChangedUpdate().acp_chunk(),
        )

    def input_keys(self, session_id: AcpSessionId) -> frozenset[InputKey]:
        """Live captured source membership, never rebuilt from durable notices."""
        state = self.state(session_id)
        following = frozenset(
            key for source in state.following.values()
            for key in source.keys
        )
        return following.union(state.turn.keys)

    async def delivery_scope(self, session_id: AcpSessionId) -> InputDeliveryScope:
        """Derive delivery notices from existing owner queues; never create new authority."""
        entry = (await self._own_entry(session_id)).entry
        owner = entry.thread
        if not owner.process_identity.runs_here() or not entry.status.is_running():
            return UNOBSERVED_DELIVERY_SCOPE
        keys = set(self.input_keys(session_id))
        keys.update(
            key
            for turn in self.state(session_id).pending_turns
            for key in turn.origin.bus_keys(owner)
        )
        return OwnerQueueInputDeliveryScope(frozenset(keys))

    async def drain_inbox(self, session_id: AcpSessionId) -> None:
        """Hand this session's undelivered messages to a turn. What it did reaches the
        session's debt as news."""
        async with self.state(session_id).lock:
            await self.drain_owned_inbox(session_id)

    async def _drain_private_if_changed(self, session_id: AcpSessionId, root_id: RootId) -> None:
        # The caller still validates the current private marker each time. The
        # ordinary loop still synchronizes configuration and schedules goals.
        state = self.state(session_id)
        seen = await self._observe_private_revision(session_id, root_id)
        if state.inbox.holds(seen):
            state.inbox = state.inbox.confirmed()
            await self.rest(session_id)
            return
        state.inbox = InboxDraining()
        try:
            await seen.drain(self, session_id, root_id)
            await state.inbox.conclude(self, session_id, root_id, seen)
        finally:
            state.inbox = state.inbox.abandoned()

    async def rest(self, session_id: AcpSessionId) -> None:
        """Idle is where the cursor refresh is owed, on the drain that reaches it as on
        every drain that finds it."""
        await self.effects.cursors.refresh(session_id, self.sessions.require(session_id))

    async def drain_owned_inbox(self, session_id: AcpSessionId) -> None:
        # The revision read below checks this root against the bus marker and
        # the checkpoint, so no separate exclusive bus read is needed here.
        await self.state(session_id).turn.resume(self.comms.owners.maintenance)
        private_root = self.comms.owners.require_private_launch().wire_root_id
        await self._drain_private_if_changed(session_id, private_root)
        WakeScheduleCheck(session_id=session_id, inputs=self).schedule()

    @property
    def background_wakes_disabled(self) -> bool:
        return not self.auto_wake

    async def accept_followup(
        self,
        session_id: AcpSessionId,
        *,
        text: str,
        display_text: str,
        request: PromptRequest,
        images: tuple[ImageInput, ...],
    ) -> PromptResponse:
        inbox = self.state(session_id).require_inbox()
        controller = self.runtime.controller.get().attached(self.sessions.client)
        followup = UserFollowupSegment.capture(text.removeprefix(AGENT_PREFIX))
        async with QueuedInput.reserve(
            self, session_id, self.sessions.require(session_id), text=display_text,
            prompt=TurnContext.render_segments((followup,), images=images).text,
            echo=request.defer_display, images=images, controller=controller,
            input_id=request.input_id, origin=request.origin,
        ) as reservation:
            item = reservation.item
            followup = replace(followup, provenance=(
                *followup.provenance, reservation.source.context_provenance(),
            ))
            rendered = TurnContext.render_segments((followup,), images=images)
            item.place(self, session_id, inbox, InputContent(
                text=item.prompt, images=tuple(images), contributions=rendered.contributions,
            ))
            request.enqueue_control(inbox, item.input_id)
            reservation.release()
        await request.publish_acceptance(self, session_id)
        return PromptResponse(
            stop_reason="end_turn",
            field_meta=encode_updates(InputDeliveryChangedUpdate()),
        )

    def require_inbox(self, session_id: AcpSessionId, inbox: TurnInbox) -> None:
        if not self.state(session_id).turn.matches(inbox):
            raise InputHandoffRefused("Running turn inbox retired before input placement")

    def bind_native_turn(
        self, session_id: AcpSessionId, owner: Thread,
    ) -> TurnInbox:
        """Open the live turn inbox and capture its configured settings."""
        inbox = self.state(session_id).open_inbox()
        self.sessions.config.turn_started(session_id, owner)
        return inbox

    async def send_now(self, session_id: AcpSessionId) -> None:
        async with self.queue_lock:
            state = self.state(session_id)
            if state.queued:
                state.to_turn(SendNow(tuple(state.queued)))

    async def close(self) -> None:
        """Retire all producers only at host shutdown."""
        self.closing = True
        await self.root_observer.stop()
        await self.close_sessions(tuple(self.drained.values()))

    async def close_sessions(self, states: tuple[SessionTaskOwner, ...]) -> None:
        tasks = tuple(task for state in states for task in state.producers())
        for state in states:
            state.retire_producers()
        for task in tasks:
            task.cancel()
        await join_retirement(asyncio.gather(*tasks, return_exceptions=True))

    async def input_refused(self, session_id: AcpSessionId, input_id: InputId) -> None:
        state = self.state(session_id)
        source = state.following.pop(input_id, None)
        if source is not None:
            await self.emit_input_delivery_changed(session_id)
        item = state.queued.pop(input_id, None)
        if item is not None:
            await item.not_started(self)
            await self.emit_queue_state(session_id)

    async def input_cleared(
        self, session_id: AcpSessionId, input_id: InputId, native_id: InputId, text: str,
    ) -> None:
        source = self.state(session_id).following.get(input_id)
        if source is not None:
            def record():
                for key in source.keys:
                    self.dispositions.clear_native(key, native_id, text)
            await Coordination.run_worker(record)
        await self.input_refused(session_id, input_id)

    def finish_original_inputs(self, keys: tuple[InputKey, ...]) -> InputDocument:
        """Commit selected input retirement before turn cleanup finishes."""
        return self.dispositions.settle_unbound(keys)

    async def finish_turn_inputs(self, following: FollowingInputRetirement) -> None:
        """Retire the ending turn's existing input owner; retain UNKNOWN only as notices."""
        session_id, inbox = following.session_id, following.inbox
        state = self.state(session_id)
        turn = state.retire_turn(inbox)
        if not turn.is_open():
            return
        self.sessions.config.settings_in_use.pop(session_id, None)
        while not inbox.empty():
            inbox.get_nowait()
        try:
            await turn.finish_original(self)
        finally:
            try:
                await following.pass_on_unread()
            finally:
                # Retire only after the old lease ended and live, unwritten
                # acceptances had their handoff. UNKNOWN rows never grant it.
                state.following = {}
                remaining, state.queued = state.queued, {}
                for item in remaining.values():
                    await item.not_started(self)
                if remaining:
                    state.restored.update(
                        {
                            key: restored
                            for key, item in remaining.items()
                            for restored in item.restore_after_turn()
                        }
                    )
        await self.emit_input_delivery_changed(session_id)
        if remaining:
            await self.emit_queue_state(session_id)
