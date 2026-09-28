"""Activity, runtime metadata and local turn lease ownership."""

from __future__ import annotations

import logging
from collections.abc import Mapping
from pathlib import Path
from typing import TYPE_CHECKING

from .registration import Registration

if TYPE_CHECKING:
    pass
from .declarations import (
    Activity,
    ActivityLog,
    ActivityState,
    AgentRuntimeInfo,
    FinishedTurnFence,
    RuntimeInfoStore,
    TurnLeaseFence,
    TurnRouting,
    _store_lock,
)

_LOG = logging.getLogger(__name__)


class AgentActivity:
    def __init__(self, root: Path, registry: Registration):
        self.registry = registry
        self._wire_lock_path = root / "wire"
        self.activity = ActivityLog(root / "activity.jsonl")
        self.runtime_info = RuntimeInfoStore(root / "runtime_info.json")

    def set_activity(self, thread: str, state: ActivityState, detail: str = "") -> None:
        """Declare a thread's current activity (thinking/working/idle)."""
        with _store_lock(self._wire_lock_path):
            canonical = self.registry.require(thread).name
            self.activity.emit(Activity(thread=canonical, state=state, detail=detail))

    def activity_of(self, thread: str) -> Activity:
        participant = self.registry.require(thread)
        return self.activity.current(participant.name, active=participant.executing)

    def all_activity(self) -> Mapping[str, Activity]:
        active = frozenset(t.name for t in self.registry.all_threads().values() if t.executing)
        return self.activity.all_current(active=active)

    def begin_turn(
        self, name: str, turn_id: str, detail: str = "", routing: TurnRouting | None = None
    ) -> TurnLeaseFence:
        with _store_lock(self._wire_lock_path):
            claimed, _ = self.registry.claim_local_turn(name, turn_id, routing=routing)
            lease = claimed.turn_lease
            assert lease is not None
            try:
                self.activity.emit(Activity(claimed.name, ActivityState.THINKING, detail))
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
            self.activity.emit(Activity(name, ActivityState.IDLE))
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
        return self.runtime_info.get(self.registry.require(thread).name)
