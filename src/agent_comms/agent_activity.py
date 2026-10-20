"""Activity, runtime metadata and local turn lease ownership."""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, replace
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING

from .locked_store import RegistryFile
from .native_input_owner import RegistryOwner
from .notification_progress import AcknowledgedProgress

if TYPE_CHECKING:
    from .agent_backend import AgentBackend
    from .agent_events import RuntimeInfoObservation
    from .bus_publication import CommittedDelivery
    from .notification_progress import AssignmentProgress
    from .presentation import MessageNotification
    from .registration import Registration
    from .registry_document import RegistrySnapshot
from .activity import (
    Activity,
    ActivityLog,
    ActivityState,
    DrainDiagnostic,
    ObservedActivity,
)
from .audience_manifest import FrozenRecipient
from .coordinator import Coordination
from .declared_family import DeclaredFamily
from .errors import RelationViolationError
from .maintenance_barrier import MaintenanceBarrier
from .routing import TurnRouting
from .runtime_info import RuntimeInfo, RuntimeInfoStore
from .store_files import SharedLock, _store_lock
from .thread_identity import OwnerIdentity, ThreadIncarnation
from .threads import Thread
from .turn_lease import NoFinishedTurn, TurnCompletion, TurnLeaseFence, TurnState
from .turn_phase import IdlePhase, PreparingPhase, TurnPhase

_LOG = logging.getLogger(__name__)


class RecipientActivity(DeclaredFamily, affix="RecipientActivity"):
    """Acquired recipient availability; no assignment, start or retry authority."""

    def turn_started_by(self, updated_at_ms: int) -> bool:
        return False

    def after_inbox_read(self, notification, source, reads, document, snapshot):
        return notification

    def displayed_recipient(self, notification, source, reads, document, snapshot):
        return reads.displayed_recipient(
            source, notification.recipient_identity, snapshot, document=document
        )

    def selected_response(
        self,
        notification: MessageNotification,
        updated_at_ms: int,
        progress: AssignmentProgress,
    ) -> MessageNotification:
        return progress.selected_notification(
            notification, busy=self.turn_started_by(updated_at_ms)
        )


@dataclass(frozen=True)
class RecordedRecipientActivity(RecipientActivity):
    """Durable history supplies no current process, readiness or inbox check."""

    def displayed_recipient(self, notification, source, reads, document, snapshot):
        return reads.historical_displayed_recipient(
            source.message, notification.recipient_identity, snapshot, document=document
        )


@dataclass(frozen=True)
class ExternalRecipientActivity(RecipientActivity):
    incarnation: ThreadIncarnation

    def after_inbox_read(self, notification, source, reads, document, snapshot):
        if self.incarnation.current(snapshot) and reads.displayed_incarnation(
            source.message, self.incarnation, snapshot, document=document
        ):
            return replace(
                notification,
                state=AcknowledgedProgress(),

                changed=True,
            )
        return notification


@dataclass(frozen=True)
class UnavailableRecipientActivity(RecipientActivity):
    pass


@dataclass(frozen=True)
class LiveRecipientActivity(RecipientActivity):
    thread: Thread
    activity: ObservedActivity

    def turn_started_by(self, updated_at_ms: int) -> bool:
        return self.thread.turn_started_by(updated_at_ms)

class AgentActivity:
    def __init__(self, root: Path, registry: Registration):
        self.registry = registry
        self._wire_lock_path = root / "wire"
        self.activity = ActivityLog(ActivityLog.path_in(root))
        self.runtime_info = RuntimeInfoStore.at(root)

    def set_activity(self, thread: str, state: ActivityState, detail: str = "") -> None:
        """Declare a thread's current activity (thinking/working/idle)."""
        if state.retires_thread:
            raise RelationViolationError("Only permanent thread deletion retires its activity.")
        with _store_lock(self._wire_lock_path, mode=SharedLock()):
            # A tool-use hook runs outside any watch; maintenance refuses it like a send.
            MaintenanceBarrier(RegistryFile.path_in(self._wire_lock_path.parent)).assert_open_unlocked()
            self._emit_activity(Activity.create(self.registry.require(thread).name, state, detail))

    def activity_of(self, thread: str, *, snapshot: RegistrySnapshot) -> ObservedActivity:
        participant = snapshot.require(thread)
        return self.all_activity((participant,), snapshot=snapshot)[participant.name]

    def is_working(self, incarnation: ThreadIncarnation, snapshot: RegistrySnapshot) -> bool:
        if not incarnation.current(snapshot):
            return False
        name = snapshot.canonical_name(incarnation.name)
        return (snapshot.status(name).running and snapshot.threads[name].process_alive
                and self.activity_of(name, snapshot=snapshot).state.busy)

    def _observed(self, participant: Thread, owner: OwnerIdentity,
                  event: Activity) -> ObservedActivity:
        activity = ObservedActivity.acquire(event, owner)
        return participant.execution.project_activity(participant, activity)

    def all_activity(self, threads: Iterable[Thread], *, snapshot: RegistrySnapshot) -> Mapping[str, ObservedActivity]:
        requested = {thread.name: thread for thread in threads}
        if not requested:
            return {}
        events = self.activity.current_for(
            requested,
            active=frozenset(name for name, thread in requested.items() if thread.executing),
        )
        claims = self.registry.storage_rule.room_ledger.claims(
            self._wire_lock_path.parent, requested.values())
        observed = {name: self._observed(thread, snapshot.owner_identity(name), events[name])
                    for name, thread in requested.items()}
        for name, claim in claims.items():
            observed[name] = claim.phase.project(observed[name], claim.work)
        return observed

    def observe_recipients(
        self, recipients: Iterable[tuple[CommittedDelivery, FrozenRecipient]], *,
        snapshot: RegistrySnapshot,
    ) -> Mapping[str, RecipientActivity]:
        """Acquire one bounded window's recipients from the original sends."""
        observations: dict[str, RecipientActivity] = {}
        for original, recipient in recipients:
            lookup = recipient.recipient_lookup
            if lookup in observations:
                continue
            selected = snapshot.recipient(recipient, original)
            observations[lookup] = selected.activity(self, snapshot)
        return observations

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
        with _store_lock(self._wire_lock_path, mode=SharedLock()):
            snapshot = self.registry.snapshot()
            if snapshot.owner_identity(thread) != owner:
                return False
            if diagnostic.owner != owner:
                raise RelationViolationError("Drain diagnostic belongs to another owner.")
            if source_error is not None:
                from .diagnostics import record_drain_failure

                path = record_drain_failure(
                    self._wire_lock_path.parent,
                    thread=thread, diagnostic=diagnostic, source_error=source_error,
                )
                diagnostic = replace(diagnostic, reason=f"{source_error} · Diagnostic: {path}")
            participant = snapshot.threads[owner.incarnation.name]

            def record(event: Activity) -> Activity | None:
                current = self._observed(participant, owner, event)
                if current.readiness.source_diagnostic() == diagnostic:
                    return None
                return replace(current.source_event(), diagnostic=diagnostic, timestamp=time.time())

            return self.activity.update(participant.name, record)

    async def recover_drain(
        self, thread: str, owner: OwnerIdentity, observed: ObservedActivity,
    ) -> bool:
        """Clear only the original failure observed before a successful drain.

        Another observer can record a different error while this drain awaits
        work. A later occurrence of the same error also has its own original
        observation time; neither may be cleared by this earlier operation.
        """
        expected = observed.readiness.source_diagnostic()
        if expected is None or expected.owner != owner:
            return False
        return await Coordination.run_worker(partial(self._recover_drain, thread, owner, expected))

    def _recover_drain(self, thread: str, owner: OwnerIdentity, expected: DrainDiagnostic) -> bool:
        with _store_lock(self._wire_lock_path, mode=SharedLock()):
            snapshot = self.registry.snapshot()
            if snapshot.owner_identity(thread) != owner:
                return False
            participant = snapshot.threads[owner.incarnation.name]

            def recover(event: Activity) -> Activity | None:
                current = self._observed(participant, owner, event)
                diagnostic = current.readiness.source_diagnostic()
                if diagnostic != expected or diagnostic.recorded_at != expected.recorded_at:
                    return None
                return replace(current.source_event(), diagnostic=None, timestamp=time.time())

            return self.activity.update(participant.name, recover)

    def recover_completed_turn(self, lease: TurnLeaseFence, backend: type[AgentBackend]) -> bool:
        """A successful later backend turn disproves only an older drain failure."""
        snapshot = self.registry.snapshot()
        thread = snapshot.require(lease.identity.incarnation.name)
        if thread.turn_lease != lease.renamed(thread.name) or thread.backend != backend:
            return False
        owner = snapshot.owner_identity(thread.name)
        diagnostic = self.activity_of(thread.name, snapshot=snapshot).readiness.source_diagnostic()
        if diagnostic is None or diagnostic.recorded_at >= lease.record.read().started_at:
            return False
        return self._recover_drain(thread.name, owner, diagnostic)

    def begin_turn(
        self, name: str, turn_id: str, detail: str = "", routing: TurnRouting | None = None
    ) -> RegistryOwner:
        """Return the exact owner installed by this atomic begin, including its lease."""
        with _store_lock(self._wire_lock_path):
            leased, _ = self.registry.lease_local_turn(name, turn_id, routing=routing)
            lease = leased.turn_lease
            assert lease is not None
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
        with _store_lock(self._wire_lock_path, mode=SharedLock()):
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
        with _store_lock(self._wire_lock_path):
            released, fence = self.registry.release_turn(lease)
            if not released:
                return NoFinishedTurn()
            self._emit_activity(IdlePhase().activity_event(lease.identity.incarnation.name))
            return fence

    def observe_runtime_info(self, thread: str, observation: RuntimeInfoObservation) -> None:
        """Serialize the original observation with thread rename/retirement."""
        with _store_lock(self._wire_lock_path, mode=SharedLock()):
            canonical = self.registry.require(thread).name
            self.runtime_info.observe(canonical, time.time(), observation)

    def agent_info_of(self, thread: str) -> RuntimeInfo:
        return self.runtime_info.for_thread(self.registry.require(thread).name)
