"""Activity, runtime metadata and local turn lease ownership."""

from __future__ import annotations

import logging
import time
from abc import abstractmethod
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING

from .registration import Registration

if TYPE_CHECKING:
    from .presentation import MessageNotification
from .activity import (
    Activity, ActivityLog, ActivityState, DrainDiagnostic, ObservedActivity,
)
from .declared_family import DeclaredFamily
from .audience_manifest import FrozenRecipient
from .errors import RelationViolationError
from .registry_document import RegistrySnapshot
from .routing import TurnRouting
from .runtime_info import AgentRuntimeInfo, RuntimeInfoStore
from .store_files import _store_lock
from .thread_identity import OwnerIdentity
from .threads import Thread
from .turn_lease import FinishedTurnFence, TurnLeaseFence
from .turn_phase import PreparingPhase, TurnPhase

_LOG = logging.getLogger(__name__)


class RecipientActivity(DeclaredFamily, affix="RecipientActivity"):
    """Acquired recipient availability; no assignment, start or retry authority."""

    def turn_started_by(self, updated_at_ms: int) -> bool:
        return False

    @abstractmethod
    def pending_notification(self, notification: MessageNotification, *, blocked_by_prior: bool) -> MessageNotification: ...


@dataclass(frozen=True)
class UnavailableRecipientActivity(RecipientActivity):
    def pending_notification(self, notification: MessageNotification, *, blocked_by_prior: bool) -> MessageNotification:
        from .presentation import MessageNotification

        return MessageNotification(notification.recipient_identity, "Waiting for agent",
            "Agent is stopped; this message has not been checked.", priority=4)


@dataclass(frozen=True)
class LiveRecipientActivity(RecipientActivity):
    thread: Thread
    activity: ObservedActivity

    def turn_started_by(self, updated_at_ms: int) -> bool:
        return self.thread.turn_started_by(updated_at_ms)

    def pending_notification(self, notification: MessageNotification, *, blocked_by_prior: bool) -> MessageNotification:
        from .presentation import MessageNotification

        if blocked_by_prior:
            notification = MessageNotification(notification.recipient_identity,
                "Queued behind current turn" if self.thread.executing else "Blocked by earlier turn",
                "The agent is finishing an earlier turn; this message has not started."
                if self.thread.executing else "An earlier turn has an unresolved outcome. This message "
                "is saved and has not started; the earlier turn needs recovery, not a resend.")
        return self.activity.readiness.pending_notification(notification)


class AgentActivity:
    def __init__(self, root: Path, registry: Registration):
        self.registry = registry
        self._wire_lock_path = root / "wire"
        self.activity = ActivityLog(root / "activity.jsonl")
        self.runtime_info = RuntimeInfoStore(root / RuntimeInfoStore.filename)

    def set_activity(self, thread: str, state: ActivityState, detail: str = "") -> None:
        """Declare a thread's current activity (thinking/working/idle)."""
        with _store_lock(self._wire_lock_path):
            canonical = self.registry.require(thread).name
            self._emit_activity(Activity(thread=canonical, state=state, detail=detail))

    def activity_of(self, thread: str, *, snapshot: RegistrySnapshot | None = None) -> ObservedActivity:
        snapshot = snapshot or self.registry.snapshot()
        owner = snapshot.owner_identity(thread)
        participant = snapshot.threads[owner.incarnation.name]
        activity = ObservedActivity.acquire(
            self.activity.current(participant.name, active=participant.executing), owner)
        if participant.turn_state.busy:
            phase = participant.turn_state.phase
            return replace(activity, state=phase.activity_state, detail=phase.summary)
        return activity

    def all_activity(self, *, snapshot: RegistrySnapshot | None = None) -> Mapping[str, ObservedActivity]:
        snapshot = snapshot or self.registry.snapshot()
        return {
            name: self.activity_of(name, snapshot=snapshot)
            for name in snapshot.threads
        }

    def observe_recipients(self, recipients: Iterable[FrozenRecipient], *, snapshot: RegistrySnapshot) -> Mapping[str, RecipientActivity]:
        """Acquire one bounded window's recipients from the original source snapshot."""
        from .bus_publication import stable_thread_lookup

        snapshot.require_unambiguous_ownership()
        observations: dict[str, RecipientActivity] = {
            recipient.recipient_lookup: UnavailableRecipientActivity() for recipient in recipients
        }
        for thread in snapshot.threads.values():
            lookup = stable_thread_lookup(thread.created_at)
            if lookup not in observations:
                continue
            try:
                snapshot.require_active(thread.name)
                process = thread.require_process()
            except RelationViolationError:
                continue
            if not process.alive():
                continue
            observations[lookup] = LiveRecipientActivity(thread, self.activity_of(thread.name, snapshot=snapshot))
        return observations

    def _emit_activity(self, activity: Activity) -> None:
        current = self.activity_of(activity.thread)
        self.activity.emit(replace(activity, diagnostic=current.readiness.source_diagnostic()))

    def set_drain_diagnostic(
        self, thread: str, owner: OwnerIdentity, diagnostic: DrainDiagnostic | None
    ) -> bool:
        """Persist one transition, fenced to the observer's exact owner incarnation."""
        with _store_lock(self._wire_lock_path):
            snapshot = self.registry.snapshot()
            if snapshot.owner_identity(thread) != owner:
                return False
            current = self.activity_of(thread)
            if current.readiness.source_diagnostic() == diagnostic:
                return False
            self.activity.emit(
                replace(current.source_event(), diagnostic=diagnostic, timestamp=time.time())
            )
            return True

    def begin_turn(
        self, name: str, turn_id: str, detail: str = "", routing: TurnRouting | None = None
    ) -> TurnLeaseFence:
        with _store_lock(self._wire_lock_path):
            leased, _ = self.registry.lease_local_turn(name, turn_id, routing=routing)
            lease = leased.turn_lease
            assert lease is not None
            try:
                self.registry.transition_turn(lease, PreparingPhase(detail[:200]))
            except BaseException:
                self.registry.release_turn(lease)
                raise
            return lease

    def transition_turn(self, lease: TurnLeaseFence, phase: TurnPhase) -> bool:
        with _store_lock(self._wire_lock_path):
            return self.registry.transition_turn(lease, phase)

    def finish_turn(self, lease: TurnLeaseFence) -> FinishedTurnFence | None:
        """Persist this lease's terminal identity before publishing idle activity."""
        with _store_lock(self._wire_lock_path):
            released, fence = self.registry.release_turn(lease)
            if not released:
                return None
            name = self.registry.canonical_name(lease.identity.incarnation.name)
            self._emit_activity(Activity(name, ActivityState.IDLE))
            return fence

    def set_agent_info(
        self,
        thread: str,
        *,
        model: str | None = None,
        session_name: str | None = None,
        context_used: int | None = None,
        context_size: int | None = None,
    ) -> None:
        """Record the latest model and context metadata for a thread."""
        with _store_lock(self._wire_lock_path):
            canonical = self.registry.require(thread).name
            self.runtime_info.set(
                AgentRuntimeInfo(
                    thread=canonical,
                    timestamp=time.time(),
                    model=model,
                    session_name=session_name,
                    context_used=context_used,
                    context_size=context_size,
                )
            )

    def agent_info_of(self, thread: str) -> AgentRuntimeInfo | None:
        return self.runtime_info.read().get(self.registry.require(thread).name)
