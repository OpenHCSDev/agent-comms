"""Activity, runtime metadata and local turn lease ownership."""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING

from .registration import Registration

if TYPE_CHECKING:
    pass
from .activity import Activity, ActivityLog, ActivityState, DrainDiagnostic
from .registry_document import RegistrySnapshot
from .routing import TurnRouting
from .runtime_info import AgentRuntimeInfo, RuntimeInfoStore
from .store_files import _store_lock
from .thread_identity import OwnerIdentity
from .turn_lease import FinishedTurnFence, TurnLeaseFence

_LOG = logging.getLogger(__name__)


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

    def activity_of(self, thread: str, *, snapshot: RegistrySnapshot | None = None) -> Activity:
        snapshot = snapshot or self.registry.snapshot()
        owner = snapshot.owner_identity(thread)
        participant = snapshot.threads[owner.incarnation.name]
        return self.activity.current(participant.name, active=participant.executing).for_owner(
            owner
        )

    def all_activity(self, *, snapshot: RegistrySnapshot | None = None) -> Mapping[str, Activity]:
        snapshot = snapshot or self.registry.snapshot()
        active = frozenset(t.name for t in snapshot.threads.values() if t.executing)
        return {
            name: activity.for_owner(snapshot.owner_identity(name))
            for name, activity in self.activity.all_current(active=active).items()
            if name in snapshot.threads
        }

    def _emit_activity(self, activity: Activity) -> None:
        current = self.activity_of(activity.thread)
        self.activity.emit(replace(activity, diagnostic=current.diagnostic))

    def set_drain_diagnostic(
        self, thread: str, owner: OwnerIdentity, diagnostic: DrainDiagnostic | None
    ) -> bool:
        """Persist one transition, fenced to the observer's exact owner incarnation."""
        with _store_lock(self._wire_lock_path):
            snapshot = self.registry.snapshot()
            if snapshot.owner_identity(thread) != owner:
                return False
            current = self.activity_of(thread)
            if current.diagnostic == diagnostic:
                return False
            self.activity.emit(
                Activity(current.thread, current.state, current.detail, diagnostic=diagnostic)
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
                self._emit_activity(Activity(leased.name, ActivityState.THINKING, detail))
            except BaseException:
                self.registry.release_turn(lease)
                raise
            return lease

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
                    model=model,
                    session_name=session_name,
                    context_used=context_used,
                    context_size=context_size,
                )
            )

    def agent_info_of(self, thread: str) -> AgentRuntimeInfo | None:
        return self.runtime_info.read().get(self.registry.require(thread).name)
