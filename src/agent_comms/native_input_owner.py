"""Captured registry and participant ownership at irreversible native boundaries.

These are distinct generation domains. Neither capture grants authority: callers
recheck the canonical registry/SQLite snapshot under their operation's locks.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import TYPE_CHECKING

from .bus_publication import stable_thread_lookup
from .coordination_errors import StaleFence
from .coordinator import Coordination
from .errors import RelationViolationError
from .registry_document import RegistrySnapshot
from .threads import Thread

if TYPE_CHECKING:
    from .registration import Registration


@dataclass(frozen=True, kw_only=True)
class RegistryOwner:
    thread: Thread
    admission_generation: int

    def _require_current(self, actual: Thread, admission: int | None, reason: str) -> None:
        if (
            admission != self.admission_generation
            or actual.pid != os.getpid()
            or (
                actual.name,
                actual.created_at,
                actual.pid,
                actual.role,
                actual.worktree,
                actual.active_turn,
                actual.goal,
            )
            != (
                self.thread.name,
                self.thread.created_at,
                self.thread.pid,
                self.thread.role,
                self.thread.worktree,
                self.thread.active_turn,
                self.thread.goal,
            )
        ):
            raise StaleFence(reason)

    def require_snapshot(self, snapshot: RegistrySnapshot, reason: str) -> None:
        actual, status = snapshot.threads.get(self.thread.name), snapshot.statuses.get(
            self.thread.name
        )
        if actual is None or status is None or not status.active:
            raise StaleFence(reason)
        self._require_current(actual, snapshot.admission_generations.get(self.thread.name), reason)

    def require_registry(self, registry: Registration) -> None:
        try:
            actual, admission = registry.live_owner_with_admission(self.thread.name)
        except (RelationViolationError, ValueError) as error:
            raise StaleFence("recipient registry owner stopped or changed") from error
        self._require_current(actual, admission, "recipient registry owner stopped or changed")


@dataclass(frozen=True)
class ParticipantOwner:
    thread: Thread
    generation: int

    def require(self, store: Coordination, lookup: str) -> None:
        participant = store.participants.get(lookup)
        if (
            not participant.committed
            or participant.owner_thread != self.thread.name
            or participant.participant_generation != self.generation
            or stable_thread_lookup(self.thread.created_at) != lookup
            or self.thread.pid != os.getpid()
            or not self.thread.role.executable
        ):
            raise StaleFence("cohort recipient is not this live registered owner generation")
