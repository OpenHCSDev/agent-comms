"""Session input ownership: acceptance, delivery cursors, queues and wake draining."""

from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import aclosing, asynccontextmanager
from dataclasses import dataclass, field, replace
from functools import partial
from typing import TYPE_CHECKING, cast

from acp.schema import PromptResponse

from .input_key import InputKey

from .acp_extension import (
    InputDeliveryChangedUpdate,
    LiveQueueScope,
    NoQueueScope,
    PromptRequest,
    QueueChangedUpdate,
    QueueScope,
    encode_updates,
)
from .activity import FailedWakeDiagnostic
from .agent_backend import AgentBackend, ClearQueue, InputContent, SendNow, TurnInbox, TurnInboxItem
from .bus import Bus
from .child_process import join_retirement
from .cohort_schema import AcceptanceFrontier
from .comms import Comms
from .controller import SessionUpdateSink
from .coordination_cohort import next_sealed_assignment
from .coordination_database import CoordinationStore
from .coordinator import Coordination
from .cursor_owner import CursorOwner
from .drain_failures import (
    DEFERRED_DRAIN_ERRORS,
    UNAVAILABLE_DRAIN_ERRORS,
    drain_failures,
    record_drain_failure,
)
from .errors import RelationViolationError, UnregisteredThreadError
from .field_codec import InputId, RootId, ThreadLookup
from .image_input import ImageInput
from .input_attempt import (
    UNOBSERVED_DELIVERY_SCOPE,
    InputDeliveryScope,
    OwnerQueueInputDeliveryScope,
)
from .input_disposition import InputDispositions, InputDocument
from .input_effects import InputEffects
from .lifecycle_capability import Executing
from .maintenance_barrier import MaintenanceBarrier, MaintenanceIngress
from .native_input_owner import RegistryOwner
from .proven_source_coverage import SourcePrefix
from .queued_input import DeliveredInput, InputHandoffRefused, QueuedInput
from .registry_document import RegistryEntry
from .registry_store import RegistryEntryRevision
from .routing import ScheduledTurn
from .runtime import RuntimeServer
from .schedule_rules import ProjectResumeCheck, WakeOutcome, WakeScheduleCheck
from .session_lifecycle import SessionLifecycle
from .thread_identity import ClaimedIdentity
from .threads import Thread
from .turn_context import TurnContext, UserFollowupSegment
from .turn_input_source import AcceptedFollowingInput, OriginalTurnInput
from .watched import ChangeSignal, FileSource

if TYPE_CHECKING:
    from agent_comms.following_inputs import OwnerFollowingInputs

    from .turn_phase import TurnPhase

AGENT_PREFIX = "!agent "


class PrivateObservationState(ABC):
    """Only a settled private observation can suppress coordination work."""

    settled = False

    @abstractmethod
    def has_unobserved_inputs(self, bus: Bus, lookup: ThreadLookup) -> bool: ...


class NoPrivateObservation(PrivateObservationState):
    def has_unobserved_inputs(self, bus: Bus, lookup: ThreadLookup) -> bool:
        return True


@dataclass(frozen=True, slots=True)
class PrivateObservation(PrivateObservationState):
    """One drain's observation: its message cut, registry entry and own coordination work."""

    source_prefix: SourcePrefix
    entry: RegistryEntry
    work: tuple
    settled = True

    def has_unobserved_inputs(self, bus: Bus, lookup: ThreadLookup) -> bool:
        self.source_prefix.require_current(bus)
        return bool(bus.received(
            lookup, after=self.source_prefix.through_seq, limit=1, descending=False))

    async def drain(self, inputs, session_id: str, root_id: RootId) -> int:
        await DeliveredInput.recover_interrupted(inputs, session_id, root_id)
        return await inputs.effects._drain_private_nk(session_id, root_id)


class ExecutionWaitingObservation(PrivateObservation):
    """A previous execution blocks new input until its stored state changes."""

    async def drain(self, inputs, session_id: str, root_id: RootId) -> int:
        return 0


class InputObservation(ABC):
    """File-driven observation and process-local wake behavior."""

    comms: Comms
    sessions: SessionLifecycle
    effects: InputEffects
    runtime: RuntimeServer
    auto_wake: bool
    closing: bool

    @abstractmethod
    def state(self, session_id: str) -> DrainedSession: ...

    def turn_live(self, session_id: str) -> bool:
        state = self.state(session_id)
        return state.turn_open() or state.turn_task.busy

    def _retire_observer(self, session_id: str, task: asyncio.Task[None]) -> None:
        state = self.state(session_id)
        if state.drain_task is task:
            state.observation_watcher = None

    def wake_observer(self, session_id: str) -> None:
        self.ensure_live_drain(session_id)
        self.state(session_id).wake_observer()

    def register_wake(self, session_id: str, task: asyncio.Task[WakeOutcome]) -> None:
        state = self.state(session_id)
        state.wake_task = task

        def finished(completed: asyncio.Task[WakeOutcome]) -> None:
            if state.wake_task is not completed:
                return
            state.wake_task = None
            # A deferred wake has not consumed its queue or assignment. The
            # maintenance witness itself wakes the observer on READY; waking
            # again now would spin on the same closed phase.
            if completed.cancelled() or completed.exception() is not None:
                return
            completed.result().settle(self.wake_observer, session_id)

        task.add_done_callback(finished)

    def ensure_live_drain(self, session_id: str) -> None:
        """Keep one background task per session pushing inbox messages live."""
        if self.closing:
            return
        state = self.state(session_id)
        if state.observing():
            return

        task = self.runtime.background(self.observe(session_id))
        state.drain_task = task
        task.add_done_callback(
            lambda completed: self._retire_observer(session_id, completed)
        )

    async def _own_entry(self, session_id: str) -> RegistryEntryRevision:
        return await Coordination.run_worker(partial(
            self.comms.registry.entry, self.sessions.require(session_id),
        ))

    def _quiescent(self, session_id: str) -> bool:
        """No process-local work or retry is waiting on the next tick."""
        state = self.state(session_id)
        turn_live = self.turn_live(session_id)
        return (
            state.settled()
            and not state.owes_tick(
                self.effects.cursors.delivery(session_id).needs_refresh,
            )
            and not turn_live
        )

    async def observe(self, session_id: str) -> None:
        initial = await self._own_entry(session_id)
        bus = self.comms.bus.bus
        lookup = initial.entry.thread.incarnation.require_recorded().lookup
        state = self.state(session_id)
        state.forget_observation()
        sources = tuple(FileSource(path) for path in (
            *bus.source_paths(self.comms.root), *CoordinationStore.source_paths(self.comms.root),
        ))
        async with aclosing(bus.watch_sources(*sources)) as observations:
            async for watcher in observations:
                state.observation_watcher = watcher
                if not state.owes_tick(self.effects.cursors.delivery(session_id).needs_refresh):
                    changed = await Coordination.run_worker(partial(
                        state.idle_revision.has_unobserved_inputs, bus, lookup))
                    if not changed:
                        continue
                try:
                    read = await self._own_entry(session_id)
                except UNAVAILABLE_DRAIN_ERRORS as error:
                    thread = self.sessions.require(session_id)
                    snapshot = await Coordination.run_worker(self.comms.registry.snapshot)
                    await record_drain_failure(
                        self.comms, thread, snapshot.owner_identity(thread),
                        FailedWakeDiagnostic, error,
                    )
                else:
                    await self._observe_entry(session_id, read.entry, watcher)

    async def _observe_entry(
        self, session_id: str, entry: RegistryEntry, watcher: ChangeSignal,
    ) -> None:
        thread = self.sessions.require(session_id)
        try:
            async with drain_failures(self.comms, thread, entry.owner_identity):
                await self.sessions.config.apply_stored_settings(session_id)
                drained = await self.drain_inbox(session_id)
                if self.effects.turns.goals.in_standby(session_id):
                    await Coordination.run_worker(partial(
                        self.comms.goals.recover_closed_goal_wait, session_id,
                    ))
                await ProjectResumeCheck(
                    session_id=session_id, inputs=self, turns=self.effects.turns,
                ).schedule()
                await self.effects.turns.goals.schedule_goal(session_id)
                current = await Coordination.run_worker(partial(self.comms.registry.require, thread))
                if (current.turn_record.items_with(Executing, current) or self.turn_live(session_id)
                        or self._quiescent(session_id)):
                    return
                state = self.state(session_id)
                if state.self_tick_owed(
                    drained, self.effects.cursors.delivery(session_id).needs_refresh,
                    self.turn_live(session_id),
                ):
                    watcher.wake()
        except DEFERRED_DRAIN_ERRORS:
            pass

    def _private_observation_revision(
        self, session_id: str, root_id: RootId, source_prefix: SourcePrefix, entry: RegistryEntry,
        store: Coordination,
    ) -> PrivateObservation:
        """Original owner/work observations, never delivery or replay authority.

        Other owners' registry phases and SQL writes cannot change this owner's
        observation. Committed wire messages, own pending/recovered work and
        physical replacement still can. Silent context records consume bytes,
        not the message cut used to select delivery.
        """
        owner = RegistryOwner(thread=entry.thread, claimed_generation=entry.claimed_generation)
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
        maintenance = MaintenanceBarrier(self.comms.registry.store.path)._current_unlocked()
        return observation(source_prefix, entry, (
            root_id,
            self.auto_wake,
            self.sessions.runtime_enabled,
            self.sessions.bindings.get(session_id),
            self.state(session_id).turn_open(),
            self.state(session_id).turn_task.busy,
            participant,
            pending,
            cursor,
            frontier,
            (original.st_dev, original.st_ino),
            maintenance.phase, maintenance.generation,
        ))

    async def _observe_private_revision(
        self, session_id: str, root_id: RootId
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

    is_open = False
    has_source = False
    @property
    def keys(self) -> tuple[str, ...]:
        return ()

    def for_phase(self, phase: TurnPhase) -> DrainTurn:
        return self if phase.accepts_followup else NoDrainTurn()

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
    is_open = True

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
    has_source = True

    @property
    def keys(self) -> tuple[str, ...]:
        return self.source.keys

    def require_source(self) -> OriginalTurnInput:
        return self.source

    async def finish_original(self, inputs: InputDrain) -> None:
        await Coordination.run_worker(partial(inputs.finish_original_inputs, self.source.keys))


class TurnTask:
    """The session's idle task state; running tasks own their operations."""

    @property
    def busy(self) -> bool:
        return False

    async def prepare_cancel(self, transition: Callable[[], Awaitable[None]]) -> None:
        pass

    async def stop(self) -> None:
        pass

    async def abort(self, backend: AgentBackend) -> None:
        pass


@dataclass(frozen=True)
class RunningTurnTask(TurnTask):
    task: asyncio.Task[object]

    @property
    def busy(self) -> bool:
        return True

    async def prepare_cancel(self, transition: Callable[[], Awaitable[None]]) -> None:
        await transition()

    async def stop(self) -> None:
        self.task.cancel()
        await asyncio.gather(self.task, return_exceptions=True)

    async def abort(self, backend: AgentBackend) -> None:
        await backend.abort(self.task)


@dataclass(eq=False)
class DrainedSession:
    """One drained session's state, from its first use until the session ends."""

    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    turn_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    turn_task: TurnTask = field(default_factory=TurnTask)
    drain_task: asyncio.Task[None] | None = None
    wake_task: asyncio.Task[WakeOutcome] | None = None
    observation_watcher: ChangeSignal | None = None
    turn: DrainTurn = field(default_factory=NoDrainTurn)
    queued: dict[InputId, QueuedInput] = field(default_factory=dict)
    restored: dict[InputId, QueuedInput] = field(default_factory=dict)
    following: dict[InputId, AcceptedFollowingInput] = field(default_factory=dict)
    queue_revision: int = 0
    pending_turns: list[ScheduledTurn] = field(default_factory=list)
    # Only a completed, quiescent observation can suppress another scan.
    # This revision never authorizes delivery, acceptance or a native send.
    idle_revision: PrivateObservationState = field(default_factory=NoPrivateObservation)
    # A changed read or process-local wake needs a full observation.
    # A completed stable drain consumes this pending work.
    repeat_observation: bool = False

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

    def wake_observer(self) -> None:
        # A process-local change need not add a new input file.
        self.repeat_observation = True
        self.notify_observer()

    def owes_tick(self, needs_refresh: bool) -> bool:
        """Keep work owed until its producer or another observation consumes it.

        The next observation consumes repeat_observation; scheduling consumes
        pending_turns; cursor refresh consumes needs_refresh; completion of the
        wake task consumes its pending wake.
        """
        return bool(
            self.repeat_observation
            or self.pending_turns
            or needs_refresh
            or self.wake_pending()
        )

    def self_tick_owed(self, drained: int, needs_refresh: bool, turn_live: bool) -> bool:
        """Notify only when this observer can consume the work on its next tick.

        A scheduled turn and a pending wake have their own completion, which
        wakes the observer; notifying for them now would spin on the same work.
        A successful push already changed the wire, but cursor refresh still
        needs its own tick.
        """
        if needs_refresh:
            return True
        if drained or self.pending_turns or turn_live:
            return False
        return self.repeat_observation

    def producers(self) -> tuple[asyncio.Task[None], ...]:
        return tuple(task for task in (self.drain_task, self.wake_task) if task is not None)

    def retire_producers(self) -> None:
        self.drain_task = self.wake_task = self.observation_watcher = None
        self.forget_observation()
        self.repeat_observation = False

    def turn_open(self) -> bool:
        return self.turn.is_open

    def observing(self) -> bool:
        return self.drain_task is not None and not self.drain_task.done()

    def wake_pending(self) -> bool:
        return self.wake_task is not None and not self.wake_task.done()

    def forget_observation(self) -> None:
        self.idle_revision = NoPrivateObservation()

    def settled(self) -> bool:
        return self.idle_revision.settled

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


class InputDrain(InputObservation):
    def owns_execution(self, session_id: str, owner: Thread) -> bool:
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
    ):
        self.comms = comms
        self.sessions = sessions
        self.runtime = runtime
        self.effects = effects
        self.drained: dict[str, DrainedSession] = {}
        self.queue_lock = asyncio.Lock()
        self.dispositions = InputDispositions(comms.root)
        self.auto_wake = auto_wake
        self.closing = False

    def state(self, session_id: str) -> DrainedSession:
        """This session's state, created on its first use."""
        state = self.drained.get(session_id)
        if state is None:
            state = self.drained[session_id] = DrainedSession()
        return state

    def end_session(self, session_id: str) -> None:
        """Ending a session drops its state, its live producers with it."""
        state = self.drained.pop(session_id, None)
        if state is not None:
            for task in state.producers():
                task.cancel()

    def end_sessions(self) -> None:
        for session_id in tuple(self.drained):
            self.end_session(session_id)

    async def clear_queued_inputs(self, session_id: str) -> None:
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
                state.to_turn(ClearQueue(tuple(item.input_id for item in cleared)))
            state.restored = {}
        await self.emit_queue_state(session_id)

    async def queue_binding(self, session_id: str) -> QueueScope:
        try:
            owner, claim_scope = await Coordination.run_worker(partial(
                self.comms.registry.live_owner_with_claim,
                self.sessions.bindings.get(session_id, session_id),
            ))
        except (RelationViolationError, UnregisteredThreadError):
            # The unavailable observation carries no local queue authority.
            return NoQueueScope()
        return LiveQueueScope(
            session_id=session_id,
            claimed_identity=ClaimedIdentity(owner.incarnation, claim_scope),
            owner=owner.require_process(),
        )

    def next_revision(self, session_id: str) -> int:
        state = self.state(session_id)
        state.queue_revision += 1
        return state.queue_revision

    async def queue_state(self, session_id: str) -> QueueChangedUpdate:
        """The producer owns the complete exact-ID queue projection."""
        scope = await self.queue_binding(session_id)
        state = self.state(session_id)
        return QueueChangedUpdate(scope, self.next_revision(session_id), scope.projection(
            tuple(state.queued.values()), tuple(state.restored.values()),
        ))

    async def emit_queue_state(
        self, session_id: str, *, client: SessionUpdateSink | None = None,
    ) -> None:
        await (client or self.runtime).publish_update(
            session_id=session_id,
            update=(await self.queue_state(session_id)).acp_chunk(),
        )

    async def emit_input_delivery_changed(
        self, session_id: str, *,
        client: SessionUpdateSink | None = None,
    ) -> None:
        await (client or self.runtime).publish_update(
            session_id=session_id,
            update=InputDeliveryChangedUpdate().acp_chunk(),
        )

    def input_keys(self, session_id: str) -> frozenset[InputKey]:
        """Live captured source membership, never rebuilt from durable notices."""
        state = self.state(session_id)
        following = frozenset(
            key for source in state.following.values()
            for key in source.keys
        )
        return following.union(state.turn.keys)

    async def delivery_scope(self, session_id: str) -> InputDeliveryScope:
        """Derive delivery notices from existing owner queues; never create new authority."""
        entry = (await self._own_entry(session_id)).entry
        owner = entry.thread
        if not owner.process_identity.runs_here() or not entry.status.running:
            return UNOBSERVED_DELIVERY_SCOPE
        keys = set(self.input_keys(session_id))
        keys.update(
            key
            for turn in self.state(session_id).pending_turns
            for key in turn.origin.bus_keys(self.dispositions, owner)
        )
        return OwnerQueueInputDeliveryScope(frozenset(keys))

    async def drain_inbox(self, session_id: str) -> int:
        """Push undelivered messages to the client; returns count pushed."""
        async with self.state(session_id).lock:
            return await self.drain_owned_inbox(session_id)

    async def _drain_private_if_changed(self, session_id: str, root_id: RootId) -> int:
        # The caller still validates the current private marker each time. The
        # ordinary loop still synchronizes configuration and schedules goals.
        state = self.state(session_id)
        before = await self._observe_private_revision(session_id, root_id)
        if state.idle_revision != before:
            state.forget_observation()
            result = await before.drain(self, session_id, root_id)
            # Never absorb a message/owner/recovery change during a suspended read,
            # or skip queued independent work after a completed native turn.
            if result:
                state.repeat_observation = False
                return result
            if before != await self._observe_private_revision(session_id, root_id):
                state.repeat_observation = True
                return 0
            state.idle_revision = before
        # Idle is where the cursor refresh is owed, on the drain that reaches it as on
        # every drain that finds it.
        state.repeat_observation = False
        await self.effects.cursors.refresh(session_id, self.sessions.require(session_id))
        return 0

    async def drain_owned_inbox(self, session_id: str) -> int:
        # The revision read below checks this root against the bus marker and
        # the checkpoint, so no separate exclusive bus read is needed here.
        await self.state(session_id).turn.resume(self.comms.owners.maintenance)
        private_root = self.comms.owners.require_private_launch().wire_root_id
        pushed = await self._drain_private_if_changed(session_id, private_root)
        WakeScheduleCheck(session_id=session_id, inputs=self).schedule()
        return pushed

    @property
    def background_wakes_disabled(self) -> bool:
        return not self.auto_wake or not self.sessions.runtime_enabled

    async def accept_followup(
        self,
        session_id: str,
        *,
        text: str,
        display_text: str,
        request: PromptRequest,
        images: tuple[ImageInput, ...],
    ) -> PromptResponse:
        inbox = self.state(session_id).require_inbox()
        controller = self.runtime.controller.get()
        if controller is not None:
            controller = controller.attached(self.sessions.client)
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

    def require_inbox(self, session_id: str, inbox: TurnInbox) -> None:
        if not self.state(session_id).turn.matches(inbox):
            raise InputHandoffRefused("Running turn inbox retired before input placement")

    def bind_native_turn(
        self, session_id: str, owner: Thread,
    ) -> TurnInbox:
        """Open the live turn inbox and capture its configured settings."""
        inbox = self.state(session_id).open_inbox()
        self.sessions.config.turn_started(session_id, owner)
        return inbox

    async def send_now(self, session_id: str) -> None:
        async with self.queue_lock:
            state = self.state(session_id)
            if state.queued:
                state.to_turn(SendNow(tuple(state.queued)))

    async def close(self) -> None:
        """Retire input producers; their turns settle from the runner's owner stop."""
        self.closing = True
        tasks = tuple(task for state in self.drained.values() for task in state.producers())
        for state in self.drained.values():
            state.retire_producers()
        for task in tasks:
            task.cancel()
        await join_retirement(asyncio.gather(*tasks, return_exceptions=True))

    async def input_refused(self, session_id: str, input_id: InputId) -> None:
        state = self.state(session_id)
        source = state.following.pop(input_id, None)
        if source is not None:
            await self.emit_input_delivery_changed(session_id)
        item = state.queued.pop(input_id, None)
        if item is not None:
            await item.not_started(self)
            await self.emit_queue_state(session_id)

    async def input_cleared(
        self, session_id: str, input_id: InputId, native_id: InputId, text: str,
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

    async def finish_turn_inputs(self, following: OwnerFollowingInputs) -> None:
        """Retire the ending turn's existing input owner; retain UNKNOWN only as notices."""
        session_id, inbox = following.session_id, following.inbox
        state = self.state(session_id)
        turn = state.retire_turn(inbox)
        if not turn.is_open:
            return
        self.sessions.config.settings_in_use.pop(session_id, None)
        while not inbox.empty():
            inbox.get_nowait()
        try:
            await turn.finish_original(self)
        finally:
            try:
                await following.pass_on_unread(self.effects.turns)
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
