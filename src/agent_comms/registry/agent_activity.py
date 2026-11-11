"""Activity, runtime metadata and local turn lease ownership."""

from __future__ import annotations

from agent_comms.execution.runtime_info_projection import RuntimeInfoProjection
from agent_comms.execution.runtime_info_source import RuntimeInfoWrites
from agent_comms.messaging.assignment_notice import AssignmentNotice
from agent_comms.messaging.delivery_recipients import DeliveryRecipients

import logging
from abc import ABC, abstractmethod
import time
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, replace
from functools import partial
from pathlib import Path

from agent_comms.agent_backend import AgentBackend
from agent_comms.coordination.coordinator import Coordination
from agent_comms.errors import RelationViolationError
from agent_comms.execution.diagnostics import record_drain_failure
from agent_comms.execution.native_input_owner import RegistryOwner
from agent_comms.execution.runtime_info import AgentRuntimeInfo, RuntimeInfo, RuntimeInfoStore
from agent_comms.execution.turn_lease import (
    NoFinishedTurn,
    StoredTurnRecord,
    TurnCompletion,
    TurnDeclaration,
    TurnLeaseFence,
    TurnRecord,
    TurnState,
)
from agent_comms.execution.turn_phase import IdlePhase, PreparingPhase, TurnPhase
from agent_comms.messaging.frozen_recipient import FrozenRecipient
from agent_comms.messaging.notification_progress import (
    DeliveryProgress,
    TakenProgress,
    WorkingTakenProgress,
)
from agent_comms.messaging.routing import NoTurnRouting, Routing
from agent_comms.nominal.field_codec import ThreadLookup, TurnId
from agent_comms.projects.storage_rules import StorageRule
from agent_comms.registry.activity import (
    Activity,
    ActivityCapture,
    ActivityState,
    ActivityStore,
    DrainDiagnostic,
    DrainObservation,
    NoDrainDiagnostic,
    ObservedActivity,
)
from agent_comms.registry.activity_registry_reads import ActivityRegistryRead
from agent_comms.registry.recipient_activity import RecipientActivity
from agent_comms.registry.registration_reads import ActivityRegistration
from agent_comms.registry.thread_identity import OwnerIdentity, ThreadIncarnation
from agent_comms.registry.thread_status import ThreadStatus
from agent_comms.registry.threads import NativeThread, Thread


_LOG = logging.getLogger(__name__)


@dataclass(frozen=True)
class LiveRecipientActivity(RecipientActivity):
    thread: Thread
    activity: ObservedActivity

    def turn_started_by(self, updated_at_ms: int) -> bool:
        return self.thread.turn_started_by(updated_at_ms)

    def taken_response(
        self, notification: AssignmentNotice[DeliveryProgress], progress: DeliveryProgress,
    ) -> AssignmentNotice[DeliveryProgress]:
        state = (WorkingTakenProgress if self.activity.state.busy else TakenProgress)(progress)
        return notification.with_state(state)

class ActivityAnswers(ABC):
    """Liveness and work answers derived from one thread's observed activity. A live store
    reads it per question; a captured read answers a whole roster from one capture."""

    @abstractmethod
    def activity_of(self, thread: str, *, snapshot: ActivityRegistryRead) -> ObservedActivity: ...

    def is_live(self, incarnation: ThreadIncarnation, snapshot: ActivityRegistryRead) -> bool:
        """The registered presence owns liveness for this exact incarnation."""
        return incarnation.current(snapshot) and self.presence(incarnation, snapshot).is_running()

    def answers_later(self, incarnation: ThreadIncarnation, snapshot: ActivityRegistryRead) -> bool:
        """Whether a wait on this exact incarnation is answered once it reads its inbox."""
        return (incarnation.current(snapshot)
                and self.presence(incarnation, snapshot).answers_later)

    @staticmethod
    def presence(incarnation: ThreadIncarnation, snapshot: ActivityRegistryRead) -> ThreadStatus:
        return snapshot.status(snapshot.canonical_name(incarnation.name))

    def is_working(self, incarnation: ThreadIncarnation, snapshot: ActivityRegistryRead) -> bool:
        return self.is_live(incarnation, snapshot) and self.activity_of(
            snapshot.canonical_name(incarnation.name), snapshot=snapshot,
        ).state.busy


@dataclass(frozen=True, slots=True)
class CapturedActivity(ActivityAnswers):
    """Every registered thread's activity from one capture, answering a whole roster."""

    observed: Mapping[str, ObservedActivity]

    def activity_of(self, thread: str, *, snapshot: ActivityRegistryRead) -> ObservedActivity:
        return self.observed[snapshot.require(thread).name]


class AgentActivity(ActivityAnswers, RuntimeInfoWrites[RuntimeInfoProjection[AgentRuntimeInfo]]):
    def __init__(self, root: Path, registry: ActivityRegistration[Thread, NativeThread, ActivityRegistryRead, StorageRule, Routing, TurnLeaseFence, TurnCompletion]):
        self.registry = registry
        self.activity = ActivityStore(root)
        self.runtime_info = RuntimeInfoStore.at(root)

    def set_activity(self, thread: str, state: ActivityState, detail: str = "") -> None:
        """Declare a thread's current activity (thinking/working/idle)."""
        if state.retires_thread:
            raise RelationViolationError("Only permanent thread deletion retires its activity.")
        self._emit_activity(Activity.create(self.registry.require(thread).name, state, detail))

    def enter_phase(self, thread: str, phase: TurnPhase) -> None:
        """Native turns, bridges and seated subagents publish the phase's own activity."""
        self.set_activity(thread, phase.activity_state, " ".join(phase.activity_detail.split()))

    def activity_of(self, thread: str, *, snapshot: ActivityRegistryRead) -> ObservedActivity:
        participant = snapshot.require(thread)
        return self.all_activity((participant,), snapshot=snapshot)[participant.name]

    def _observed(self, participant: Thread, owner: OwnerIdentity, event: Activity,
                  turns: Mapping[StoredTurnRecord, TurnDeclaration]) -> ObservedActivity:
        activity = ObservedActivity.acquire(event, owner)
        return participant.project_activity(activity, turns)

    def all_activity(self, threads: Iterable[Thread], *, snapshot: ActivityRegistryRead) -> Mapping[str, ObservedActivity]:
        return self.captured(threads, snapshot=snapshot).activity

    def captured(self, threads: Iterable[Thread], *,
                 snapshot: ActivityRegistryRead) -> ActivityCapture[ObservedActivity]:
        requested = {thread.name: thread for thread in threads}
        if not requested:
            return ActivityCapture.empty()
        events = self._events(requested, snapshot)
        claims = self.registry.storage_rule.room_ledger.claims(
            self.registry.store.path.parent, requested.values())
        turns = TurnRecord.read_all(thread.turn_record for thread in requested.values())
        observed = {name: self._observed(thread, snapshot.owner_identity(name), events.activity[name],
                                         turns)
                    for name, thread in requested.items()}
        for name, claim in claims.items():
            observed[name] = claim.phase.project(observed[name], claim.work)
        for name, oldest in self.stalled(snapshot.claimed_readers(requested), snapshot).items():
            observed[name] = replace(
                observed[name], readiness=observed[name].readiness.stalled(oldest))
        return ActivityCapture(observed, events.expires_at)

    def _events(self, requested: Mapping[str, Thread],
                snapshot: ActivityRegistryRead) -> ActivityCapture[Activity]:
        return self.activity.captured_for(
            requested,
            active=frozenset(name for name, thread in requested.items()
                             if thread.holds_activity(snapshot.status(name))),
        )

    def drain_observation(self, thread: str, *, snapshot: ActivityRegistryRead) -> DrainObservation:
        """The drain diagnostic recorded for this thread's owner. Only the recorded event
        carries it: a stall never replaces a recorded diagnostic and reports none of its
        own, so a drain reads the event, not the whole activity capture with its inbox scan."""
        participant = snapshot.require(thread)
        event = self._events({participant.name: participant}, snapshot).activity[participant.name]
        return ObservedActivity.acquire(
            event, snapshot.owner_identity(participant.name)).readiness.source_diagnostic()

    def stalled(self, readers: tuple[str, ...], snapshot: ActivityRegistryRead) -> Mapping[str, float]:
        """Each claimed reader that stopped taking its inbox, by when. A store without the bus
        reads no inbox, so it sees no stall; Comms's store reads the taken-input inbox."""
        return {}

    def observe_recipients(
        self,
        recipients: Iterable[tuple[DeliveryRecipients, FrozenRecipient]],
        *,
        snapshot: ActivityRegistryRead,
    ) -> Mapping[ThreadLookup, RecipientActivity]:
        """Acquire one bounded window's recipients from the original sends."""
        selected = {}
        for original, recipient in recipients:
            lookup = recipient.recipient_lookup
            if lookup not in selected:
                selected[lookup] = snapshot.recipient(recipient, original)
        captured = self.all_activity(
            (thread for recipient in selected.values() for thread in recipient.activity_threads()),
            snapshot=snapshot,
        )
        return {lookup: recipient.activity(captured, snapshot)
                for lookup, recipient in selected.items()}

    def _emit_activity(self, activity: Activity) -> None:
        snapshot = self.registry.snapshot()
        owner = snapshot.owner_identity(activity.thread)
        activity = replace(activity, thread=owner.incarnation.name)
        self.activity.update(activity.thread, lambda current: replace(
            activity, diagnostic=ObservedActivity.acquire(current, owner).readiness.source_diagnostic(),
        ))

    def set_drain_diagnostic(
        self, thread: str, owner: OwnerIdentity, diagnostic: DrainDiagnostic,
        *, source_error: BaseException | None = None,
    ) -> bool:
        """Persist one transition, fenced to the observer's exact owner incarnation."""
        snapshot = self.registry.snapshot()
        if snapshot.owner_identity(thread) != owner:
            return False
        if diagnostic.owner != owner:
            raise RelationViolationError("Drain diagnostic belongs to another owner.")
        if source_error is not None:


            path = record_drain_failure(
                self.registry.store.path.parent,
                thread=thread, diagnostic=diagnostic, source_error=source_error,
            )
            diagnostic = replace(diagnostic, reason=f"{source_error} · Diagnostic: {path}")
        participant = snapshot.threads[owner.incarnation.name]

        def record(event: Activity) -> Activity | None:
            current = self._observed(participant, owner, event,
                                     TurnRecord.read_all((participant.turn_record,)))
            if current.readiness.source_diagnostic() == diagnostic:
                return None
            return replace(current.source_event(), diagnostic=diagnostic, timestamp=time.time())

        return self.activity.update(participant.name, record)

    async def recover_drain(
        self, thread: str, owner: OwnerIdentity, expected: DrainObservation,
    ) -> bool:
        """Clear only the original failure observed before a successful drain.

        Another observer can record a different error while this drain awaits
        work. A later occurrence of the same error also has its own original
        observation time; neither may be cleared by this earlier operation.
        """
        if not expected.recorded_by(owner):
            return False
        return await Coordination.run_worker(partial(self._recover_drain, thread, owner, expected))

    def _recover_drain(
        self, thread: str, owner: OwnerIdentity, expected: DrainObservation,
    ) -> bool:
        snapshot = self.registry.snapshot()
        if snapshot.owner_identity(thread) != owner:
            return False
        participant = snapshot.threads[owner.incarnation.name]

        def recover(event: Activity) -> Activity | None:
            current = self._observed(participant, owner, event,
                                     TurnRecord.read_all((participant.turn_record,)))
            diagnostic = current.readiness.source_diagnostic()
            if not diagnostic.same_occurrence(expected):
                return None
            return replace(
                current.source_event(), diagnostic=NoDrainDiagnostic(), timestamp=time.time()
            )

        return self.activity.update(participant.name, recover)

    def recover_completed_turn(self, lease: TurnLeaseFence, backend: type[AgentBackend]) -> bool:
        """A successful later backend turn disproves only an older drain failure."""
        snapshot = self.registry.snapshot()
        thread = snapshot.require(lease.identity.incarnation.name)
        if thread.turn_lease != lease or thread.backend != backend:
            return False
        owner = snapshot.owner_identity(thread.name)
        diagnostic = self.drain_observation(thread.name, snapshot=snapshot)
        if not diagnostic.recorded_before(lease.record.read().started_at):
            return False
        return self._recover_drain(thread.name, owner, diagnostic)

    def begin_turn(
        self, name: str, turn_id: TurnId, detail: str = "", routing: Routing = NoTurnRouting()
    ) -> RegistryOwner:
        """Return the exact owner installed by this atomic begin, including its lease."""
        leased, _ = self.registry.lease_local_turn(name, turn_id, routing=routing)
        lease = leased.require_turn_lease()
        try:
            phase = PreparingPhase(detail)
            lease.record.transition(lease, phase)
            self._emit_activity(phase.activity_event(leased.name))
        except BaseException:
            self.registry.release_turn(lease)
            raise
        return RegistryOwner(thread=leased, claimed_generation=lease.claimed_generation)

    def _turn_phase_effects(
        self, lease: TurnLeaseFence, phase: TurnPhase,
        transition: Callable[[TurnLeaseFence, TurnPhase], tuple[TurnState, ...]],
    ) -> tuple[TurnState, ...]:
        effects = transition(lease, phase)
        for state in effects:
            state.active.phase.phase_effects(self, lease)
        return effects

    def transition_turn(self, lease: TurnLeaseFence, phase: TurnPhase) -> tuple[TurnState, ...]:
        return self._turn_phase_effects(lease, phase, lease.record.transition)

    def observe_native_phase(self, lease: TurnLeaseFence, phase: TurnPhase) -> tuple[TurnState, ...]:
        return self._turn_phase_effects(lease, phase, lease.record.observe_native_phase)

    def finish_turn(self, lease: TurnLeaseFence) -> TurnCompletion:
        """Persist this lease's terminal identity before publishing idle activity."""
        released, fence = self.registry.release_turn(lease)
        if not released:
            return NoFinishedTurn()
        self._emit_activity(IdlePhase().activity_event(lease.identity.incarnation.name))
        return fence

    def observe_runtime_info(self, thread: str, observation: RuntimeInfoProjection[AgentRuntimeInfo]) -> None:
        """Serialize the original observation with thread rename/retirement."""
        canonical = self.registry.require(thread).name
        self.runtime_info.observe(canonical, time.time(), observation)

    def agent_info_of(self, thread: str) -> RuntimeInfo:
        return self.runtime_info.for_thread(self.registry.require(thread).name)
