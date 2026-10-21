"""Session input ownership: acceptance, delivery cursors, queues and wake draining."""

from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod
from contextlib import aclosing
from dataclasses import dataclass, field, replace
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING, Any

from acp.schema import PromptResponse

from .acp_extension import (
    InputDeliveryChangedUpdate,
    LiveQueueScope,
    NoQueueScope,
    PromptRequest,
    QueueChangedUpdate,
    QueueProjection,
    QueueScope,
    encode_updates,
)
from .activity import FailedWakeDiagnostic
from .agent_backend import ClearQueue, InputContent, InputId, SendNow, TurnInbox, TurnInboxItem
from .child_process import join_retirement
from .comms import Comms
from .turn_terminal import EndedWithoutResult, TurnTerminal
from .coordination_cohort import next_sealed_assignment
from .coordination_database import CoordinationStore
from .coordination_errors import IdentityConflict
from .coordinator import Coordination
from .cursor_owner import CursorOwner
from .drain_failures import (
    DEFERRED_DRAIN_ERRORS,
    UNAVAILABLE_DRAIN_ERRORS,
    drain_failures,
    record_drain_failure,
)
from .errors import RelationViolationError, UnregisteredThreadError
from .goal_attempts import GoalAttemptStore
from .goal_waits import GoalWaits
from .input_attempt import (
    UNOBSERVED_DELIVERY_SCOPE,
    InputAttempt,
    InputDeliveryScope,
    OwnerQueueInputDeliveryScope,
)
from .input_disposition import InputDispositions, InputDocument
from .input_effects import InputEffects
from .maintenance_barrier import MaintenanceBarrier, MaintenanceIngress, MaintenanceStatus
from .native_input_owner import RegistryOwner
from .private_bus_checkpoint import Addressed, WritableCheckpoint
from .private_path import FileObservation, FileRevision
from .queued_input import DeliveredInput, InputHandoffRefused, QueuedInput
from .registry_document import RegistryEntry
from .registry_store import RegistryEntryRevision
from .routing import ScheduledTurn
from .runtime import UNBOUND_CONTROLLER, RuntimeServer
from .schedule_rules import ProjectResumeCheck, WakeScheduleCheck
from .session_lifecycle import SessionLifecycle
from .store_files import _store_lock
from .thread_identity import ClaimedIdentity
from .threads import Thread
from .turn_context import TurnContext, UserFollowupSegment
from .turn_input_source import AcceptedFollowingInput, OriginalTurnInput
from .wire_metadata import WireMetadata
from .watched import ChangeSignal, FileSource

if TYPE_CHECKING:
    from agent_comms.following_inputs import OwnerFollowingInputs

    from .turn_phase import TurnPhase

AGENT_PREFIX = "!agent "


@dataclass(frozen=True, slots=True)
class TickRevision:
    """What an observation tick depends on, with the stores every owner writes apart.

    Every owner's entry lives in the one registry file and every owner's rows
    in the one coordination store, so their revisions change when any owner's
    part does. This owner's registry entry and its private observation of the
    coordination store say whether its own part did.
    """

    registry: tuple[FileObservation, ...]
    coordination: tuple[FileObservation, ...]
    # The shared stores every publisher's appends change: the bus log and its
    # marker, and the sealed checkpoint that records which rows address which
    # recipient. They are compared apart from this owner's own stores.
    bus: tuple[FileObservation, ...]
    checkpoint: FileObservation
    other: tuple

    @classmethod
    def paths(
        cls, root, *, registry: Path, registry_guard: Path,
        bus: Path, bus_marker: Path, auth: Path,
    ) -> TickRevisionPaths:
        """Declare every physical file read by a tick in its comparison groups."""
        return TickRevisionPaths(
            registry=(registry, registry_guard),
            coordination=(
                CoordinationStore.path_in(root),
                root / "coordination.sqlite3-wal",
            ),
            bus=(bus, bus_marker),
            checkpoint=WritableCheckpoint.path_in(root),
            goal_waits=GoalWaits.path_in(root),
            goal_attempts=GoalAttemptStore.files(
                GoalAttemptStore.path_in(root / GoalAttemptStore.root_directory)),
            auth=auth,
            maintenance=MaintenanceBarrier(registry).witness_paths,
        )


@dataclass(frozen=True, slots=True)
class TickRevisionPaths:
    """Physical tick dependencies, shared by revision capture and file watching."""

    registry: tuple[Path, ...]
    coordination: tuple[Path, ...]
    bus: tuple[Path, ...]
    checkpoint: Path
    goal_waits: Path
    goal_attempts: tuple[Path, ...]
    auth: Path
    maintenance: tuple[Path, Path]

    @property
    def other(self) -> tuple[Path, ...]:
        return self.goal_waits, *self.goal_attempts, self.auth, *self.maintenance

    def capture(self, local: tuple) -> TickRevision:
        revisions = lambda paths: tuple(FileRevision.capture(path) for path in paths)
        return TickRevision(
            registry=revisions(self.registry),
            coordination=revisions(self.coordination),
            bus=revisions(self.bus),
            checkpoint=FileRevision.capture(self.checkpoint),
            other=(revisions(self.other), *local),
        )


class PrivateObservationState(ABC):
    """Only a settled private observation can suppress coordination work."""

    settled = False

    @abstractmethod
    async def coordination_unchanged(
        self, inputs: InputDrain, session_id: str, entry: RegistryEntry,
    ) -> bool: ...


class NoPrivateObservation(PrivateObservationState):
    async def coordination_unchanged(
        self, inputs: InputDrain, session_id: str, entry: RegistryEntry,
    ) -> bool:
        return False


@dataclass(frozen=True, slots=True)
class PrivateObservation(PrivateObservationState):
    """One drain's observation: its message cut, registry entry and own coordination work."""

    wire: tuple
    entry: RegistryEntry
    work: tuple
    settled = True

    async def coordination_unchanged(
        self, inputs: InputDrain, session_id: str, entry: RegistryEntry,
    ) -> bool:
        observed = await inputs._observe_coordination(
            session_id, inputs.effects.configured_root_id, self.wire, entry,
        )
        return self == observed

    async def drain(self, inputs, session_id: str, root_id: str) -> int:
        await DeliveredInput.recover_interrupted(inputs, session_id, root_id)
        return await inputs.effects._drain_private_nk(session_id, root_id)


class ExecutionWaitingObservation(PrivateObservation):
    """A previous execution blocks new input until its stored state changes."""

    async def drain(self, inputs, session_id: str, root_id: str) -> int:
        return 0


class TickState(ABC):
    async def next(
        self, observation: InputObservation, session_id: str, revision: TickRevision,
    ) -> SettledTick | FullTick | FailedTick:
        try:
            return await self._next(observation, session_id, revision)
        except UNAVAILABLE_DRAIN_ERRORS as error:
            return FailedTick(error)

    @abstractmethod
    async def _next(
        self, observation: InputObservation, session_id: str, revision: TickRevision,
    ) -> SettledTick | FullTick: ...


class UnsettledTick(TickState):
    async def _next(
        self, observation: InputObservation, session_id: str, revision: TickRevision,
    ) -> FullTick:
        read = await observation._own_entry(session_id)
        return FullTick(replace(revision, registry=read.revision), read.entry)


@dataclass(frozen=True, slots=True)
class FailedTick(TickState):
    error: Exception

    async def _next(
        self, observation: InputObservation, session_id: str, revision: TickRevision,
    ) -> FullTick:
        return await UnsettledTick()._next(observation, session_id, revision)

    async def apply(
        self, observation: InputObservation, session_id: str, watcher: ChangeSignal,
    ) -> TickState:
        thread = observation.sessions.require(session_id)
        snapshot = await Coordination.run_worker(observation.comms.registry.snapshot)
        owner = snapshot.owner_identity(thread)
        await record_drain_failure(
            observation.comms, thread, owner, FailedWakeDiagnostic, self.error,
        )
        return self


@dataclass(frozen=True, slots=True)
class FullTick:
    revision: TickRevision
    entry: RegistryEntry

    async def apply(self, observation: InputObservation, session_id: str, watcher: ChangeSignal) -> TickState:
        thread = observation.sessions.require(session_id)
        owner = self.entry.owner_identity
        try:
            async with drain_failures(observation.comms, thread, owner):
                # Configuration changes precede messages handed over by this tick.
                await observation.sessions.config.apply_stored_settings(session_id)
                drained = await observation.drain_inbox(session_id)
                if observation.effects.turns.goals.in_standby(session_id):
                    await Coordination.run_worker(partial(
                        observation.comms.goals.recover_closed_goal_wait, session_id,
                    ))
                await ProjectResumeCheck(
                    session_id=session_id, inputs=observation, turns=observation.effects.turns,
                ).schedule()
                await observation.effects.turns.goals.schedule_goal(session_id)
                current = await Coordination.run_worker(partial(observation.comms.registry.require, thread))
                if (
                    current.turn_record.executing
                    or observation.turn_live(session_id)
                ):
                    return UnsettledTick()
                if observation._quiescent(session_id):
                    marker = await Coordination.run_worker(observation._bus_marker)
                    return SettledTick(
                        self.revision, self.entry, marker.claimed_after_seq, marker.last_seq,
                    )
                state = observation.state(session_id)
                needs_refresh = observation.effects.cursors.delivery(session_id).needs_refresh
                if state.self_tick_owed(
                    drained, needs_refresh, observation.turn_live(session_id),
                ):
                    watcher.wake()
        except DEFERRED_DRAIN_ERRORS:
            pass
        return UnsettledTick()


@dataclass(frozen=True, slots=True)
class SettledTick(TickState):
    """The revision a settled tick started from, and this owner's registry entry then.

    Read the owner's accepted-input floor from the stored marker when
    the tick settles.
    """

    revision: TickRevision
    entry: RegistryEntry
    claimed_after_seq: int | None
    through_seq: int

    async def _next(
        self, observation: InputObservation, session_id: str, revision: TickRevision,
    ) -> SettledTick | FullTick:
        if revision.other != self.revision.other or not observation._quiescent(session_id):
            return await UnsettledTick()._next(observation, session_id, revision)
        if revision.registry != self.revision.registry:
            # A standby's wait graph also depends on other threads' entries.
            if observation.effects.turns.goals.in_standby(session_id):
                return await UnsettledTick()._next(observation, session_id, revision)
            step = await UnsettledTick()._next(observation, session_id, revision)
            if step.entry != self.entry:
                return step
        else:
            step = FullTick(revision, self.entry)
        if (
            step.revision.bus == self.revision.bus
            and step.revision.checkpoint == self.revision.checkpoint
            and step.revision.coordination == self.revision.coordination
        ):
            return SettledTick(step.revision, step.entry, self.claimed_after_seq, self.through_seq)
        return await observation._shared_changes_concern_other_owners(
            session_id, self, step.revision, step.entry,
        )

    async def apply(self, observation: InputObservation, session_id: str, watcher: ChangeSignal) -> TickState:
        return self


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
        return self.state(session_id).turn_open() or session_id in self.effects.turns.turn_tasks

    def _tick_paths(self) -> TickRevisionPaths:
        configuration = self.sessions.config.configuration
        return TickRevision.paths(
            self.comms.root,
            registry=self.comms.registry.store.path,
            registry_guard=self.comms.registry.store.guard_path,
            bus=self.comms.bus.log.path,
            bus_marker=self.comms.bus.log.metadata_path,
            auth=configuration.native_config / "auth.json",
        )

    def _tick_revision(
        self, session_id: str, paths: TickRevisionPaths | None = None,
    ) -> TickRevision:
        """Stat every declared physical dependency plus process-local inputs."""
        return (paths or self._tick_paths()).capture((
            self.sessions.bindings.get(session_id),
            self.auto_wake,
            self.sessions.runtime_enabled,
        ))

    def _retire_observer(self, session_id: str, task: asyncio.Task[None]) -> None:
        state = self.state(session_id)
        if state.drain_task is task:
            state.observation_watcher = None

    def wake_observer(self, session_id: str) -> None:
        state = self.state(session_id)
        # A process-local wake can carry work that did not change an input file.
        state.repeat_observation = True
        state.notify_observer()

    def register_wake(self, session_id: str, task: asyncio.Task[None]) -> None:
        state = self.state(session_id)
        state.wake_task = task

        def finished(completed: asyncio.Task[None]) -> None:
            if state.wake_task is not completed:
                return
            state.wake_task = None
            # A deferred wake has not consumed its queue or assignment. The
            # maintenance witness itself wakes the observer on READY; waking
            # again now would spin on the same closed phase.
            if completed.cancelled() or completed.exception() is not None:
                return
            if isinstance(completed.result(), MaintenanceStatus):
                return
            self.wake_observer(session_id)

        task.add_done_callback(finished)

    def _indexed_changes_for_owner(
        self, settled: SettledTick, revision: TickRevision, lookup: str,
    ) -> tuple[WireMetadata, bool]:
        """Read this owner's address aggregate only from the captured index cut."""
        unstable = RelationViolationError("Wire address index has no stable file revision")
        previous_info = settled.revision.bus[0].require(unstable)
        current_info = revision.bus[0].require(unstable)
        checkpoint = revision.checkpoint.require(unstable)
        if previous_info.identity != current_info.identity:
            raise RelationViolationError("Wire tail does not extend the settled source")
        return Addressed.observed_after(
            self.comms.bus.log, checkpoint, current_info,
            revision.bus[1], lookup, settled.through_seq,
        )


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
        state: TickState = UnsettledTick()
        paths = self._tick_paths()
        async with aclosing(self.comms.bus.bus.watch(
            FileSource(paths.auth), *(FileSource(path) for path in paths.maintenance),
        )) as observations:
            async for watcher in observations:
                self.state(session_id).observation_watcher = watcher
                revision = await Coordination.run_worker(
                    partial(self._tick_revision, session_id)
                )
                step = await state.next(self, session_id, revision)
                state = await step.apply(self, session_id, watcher)

    def _bus_marker(self):
        return self.comms.bus.log.private_marker_unlocked()

    async def _shared_changes_concern_other_owners(
        self, session_id: str, settled: SettledTick, revision: TickRevision,
        entry: RegistryEntry,
    ) -> SettledTick | FullTick:
        """Resolve shared-store changes per owner before any row is decoded.

        Every publisher's append changes the shared bus log, its marker, the
        sealed checkpoint and the coordination store, but none of that is this
        owner's concern until the address record shows rows this owner has not
        authorized, or this owner's own coordination rows changed. The floor
        comparison and the sealed address read answer the bus and checkpoint
        components; _own_coordination_unchanged answers the coordination
        component. An unavailable answer keeps the full tick — unavailable
        never means 'not mine'.
        """
        claimed_after_seq, through_seq = settled.claimed_after_seq, settled.through_seq
        try:
            if (
                revision.bus != settled.revision.bus
                or revision.checkpoint != settled.revision.checkpoint
            ):
                marker, addressed = await Coordination.run_worker(partial(
                    self._indexed_changes_for_owner,
                    settled,
                    revision,
                    entry.thread.incarnation.lookup,
                ))
                if marker.claimed_after_seq != settled.claimed_after_seq or addressed:
                    return FullTick(revision, entry)
                claimed_after_seq, through_seq = marker.claimed_after_seq, marker.last_seq
            if revision.coordination != settled.revision.coordination:
                if not await self._own_coordination_unchanged(session_id, entry):
                    return FullTick(revision, entry)
        except UNAVAILABLE_DRAIN_ERRORS:
            return FullTick(revision, entry)
        return SettledTick(revision, entry, claimed_after_seq, through_seq)

    def _private_observation_revision(
        self, session_id: str, root_id: str, wire: tuple, entry: RegistryEntry,
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
            participant = store.participants.get(owner.thread.incarnation.lookup)
            pending = next_sealed_assignment(
                store, participant.lookup, owner.thread.name, after_seq=wire[-1],
            )
            cursor = CursorOwner(
                thread=owner.thread, claimed_generation=owner.claimed_generation,
                wire_root_id=root_id, generation=participant.participant_generation,
            ).cursor(store.session._connection)
            original = store.session.path.stat()
        observation = PrivateObservation
        if (
            participant.pointer.unresolved
            and not self.owns_execution(session_id, owner.thread)
        ):
            observation = ExecutionWaitingObservation
        return observation(wire, entry, (
            root_id,
            self.auto_wake,
            self.sessions.runtime_enabled,
            self.sessions.bindings.get(session_id),
            self.state(session_id).turn_open(),
            session_id in self.effects.turns.turn_tasks,
            participant,
            pending,
            cursor,
            (original.st_dev, original.st_ino),
            tuple(FileRevision.capture(path) for path in
                  MaintenanceBarrier(self.comms.registry.store.path).witness_paths),
        ))

    async def _observe_private_revision(self, session_id: str, root_id: str) -> PrivateObservation:
        # Pending acquisition is not an inbox failure or a completed read.
        # Cancellation retires only this observation's waiting descriptor.
        def wire_revision(source):
            if source.marker.root_id != root_id:
                raise IdentityConflict("private drain original root changed")
            return (
                source.witness.source_identity,
                source.committed_sequence(),
                source.marker.claimed_after_seq,
            )
        wire = await self.comms.bus.log.read_checked_async(wire_revision)
        entry = (await self._own_entry(session_id)).entry
        return await self._observe_coordination(session_id, root_id, wire, entry)

    async def _observe_coordination(
        self, session_id: str, root_id: str, wire: tuple, entry: RegistryEntry,
    ) -> PrivateObservation:
        # Release all bus locks before starting registry/SQL consumer work.
        # The original coordinator owns its bounded read acquisition policy;
        # the observation must not replace it with a zero-wait failure.
        return await Coordination.run_read_async(
            CoordinationStore.path_in(self.comms.root),
            partial(self._private_observation_revision, session_id, root_id, wire, entry),
        )

    async def _own_coordination_unchanged(self, session_id: str, entry: RegistryEntry) -> bool:
        """Only the coordination store changed since this owner's last idle drain: are its rows?

        The caller's tick found the bus log, its marker and checkpoint unchanged
        and holds the current registry entry, so the settled message cut and
        this entry still stand; only this owner's coordination rows are read.
        """
        async with self.state(session_id).lock:
            return await self.state(session_id).idle_revision.coordination_unchanged(
                self, session_id, entry,
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


@dataclass(eq=False)
class DrainedSession:
    """One drained session's state, from its first use until the session ends."""

    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    drain_task: asyncio.Task[None] | None = None
    wake_task: asyncio.Task[None] | None = None
    observation_watcher: ChangeSignal | None = None
    turn: DrainTurn = field(default_factory=NoDrainTurn)
    queued: dict[str, QueuedInput] = field(default_factory=dict)
    restored: dict[str, QueuedInput] = field(default_factory=dict)
    following: dict[str, AcceptedFollowingInput] = field(default_factory=dict)
    queue_revision: int = 0
    pending_turns: list[ScheduledTurn] = field(default_factory=list)
    # Only a completed, quiescent observation can suppress another scan.
    # This revision never authorizes delivery, acceptance or a native send.
    idle_revision: PrivateObservationState = field(default_factory=NoPrivateObservation)
    # A changed read or process-local wake needs a full observation.
    # A completed stable drain consumes this pending work.
    repeat_observation: bool = False

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

    def turn_started(self) -> bool:
        return self.turn.has_source

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
                state.to_turn(ClearQueue(tuple(InputId(item.input_id) for item in cleared)))
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
            session_id,
            ClaimedIdentity(owner.incarnation, claim_scope),
            owner.require_process().pid,
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

    async def emit_queue_state(self, session_id: str, *, client: Any = None) -> None:
        await (client or self.runtime).session_update(
            session_id=session_id,
            update=(await self.queue_state(session_id)).acp_chunk(),
        )

    async def emit_input_disposition(
        self, session_id: str, row: InputAttempt, client: Any = None
    ) -> None:
        await self.emit_input_delivery_changed(session_id, public_id=row.public_id, client=client)

    async def emit_input_delivery_changed(
        self, session_id: str, *, public_id: str | None = None, client: Any = None
    ) -> None:
        await (client or self.runtime).session_update(
            session_id=session_id,
            update=InputDeliveryChangedUpdate(public_id).acp_chunk(),
        )

    def input_keys(self, session_id: str) -> frozenset[str]:
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
            self.dispositions.bus_key(turn.origin, owner)
            for turn in self.state(session_id).pending_turns
            if turn.origin is not None
        )
        return OwnerQueueInputDeliveryScope(frozenset(keys))

    async def drain_inbox(self, session_id: str) -> int:
        """Push undelivered messages to the client; returns count pushed."""
        async with self.state(session_id).lock:
            return await self.drain_owned_inbox(session_id)

    async def _drain_private_if_changed(self, session_id: str, root_id: str) -> int:
        # The caller still validates the current private marker each time. The
        # ordinary loop still synchronizes configuration and schedules goals.
        state = self.state(session_id)
        before = await self._observe_private_revision(session_id, root_id)
        if state.idle_revision == before:
            state.repeat_observation = False
            await self.effects.cursors.refresh(session_id, self.sessions.require(session_id))
            return 0
        state.forget_observation()
        result = await before.drain(self, session_id, root_id)
        # Never absorb a message/owner/recovery change during a suspended read,
        # or skip queued independent work after a completed native turn.
        if result == 0:
            if before == await self._observe_private_revision(session_id, root_id):
                state.idle_revision = before
                state.repeat_observation = False
            else:
                state.repeat_observation = True
        else:
            state.repeat_observation = False
        return result

    async def drain_owned_inbox(self, session_id: str) -> int:
        # The revision read below checks this root against the bus marker and
        # the checkpoint, so no separate exclusive bus read is needed here.
        await self.state(session_id).turn.resume(self.comms.owners.maintenance)
        private_root = self.effects.configured_root_id
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
        images: tuple[Any, ...],
    ) -> PromptResponse:
        inbox = self.state(session_id).require_inbox()
        controller = self.runtime.controller.get()
        if controller is UNBOUND_CONTROLLER:
            controller = self.sessions.client
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
            field_meta=encode_updates(InputDeliveryChangedUpdate(item.input_id)),
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
                state.to_turn(SendNow(tuple(InputId(input_id) for input_id in state.queued)))

    async def close(self, terminal: TurnTerminal = EndedWithoutResult()) -> None:
        """Retire input producers with the shutdown's actual terminal cause."""
        self.closing = True
        tasks = tuple(task for state in self.drained.values() for task in state.producers())
        for task in tasks:
            task.cancel(terminal)
        try:
            await join_retirement(asyncio.gather(*tasks, return_exceptions=True))
        finally:
            for state in self.drained.values():
                state.retire_producers()

    async def input_refused(self, session_id: str, input_id: str) -> None:
        state = self.state(session_id)
        source = state.following.pop(input_id, None)
        if source is not None:
            await self.emit_input_delivery_changed(session_id)
        item = state.queued.pop(input_id, None)
        if item is not None:
            await item.not_started(self)
            await self.emit_queue_state(session_id)

    async def input_cleared(
        self, session_id: str, input_id: str, native_id: str, text: str,
    ) -> None:
        source = self.state(session_id).following.get(input_id)
        if source is not None:
            def record():
                with _store_lock(self.comms._wire_lock_path):
                    for key in source.keys:
                        self.dispositions.clear_native(key, native_id, text)
            await Coordination.run_worker(record)
        await self.input_refused(session_id, input_id)

    def finish_original_inputs(self, keys: tuple[str, ...]) -> InputDocument:
        """Settle the selected inputs under the bus lock before turn cleanup finishes."""
        with _store_lock(self.comms._wire_lock_path):
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
