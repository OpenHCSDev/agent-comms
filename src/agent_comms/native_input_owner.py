"""Captured registry and participant ownership at irreversible native boundaries.

These are distinct generation domains. Neither capture grants authority: callers
recheck the canonical registry/SQLite snapshot under their operation's locks.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import TYPE_CHECKING, ClassVar

from .bus_publication import stable_thread_lookup
from .child_process import ProcessIdentity
from .coordination_errors import StaleFence
from .coordination_tables.participants import OwnerGenerations
from .coordinator import Coordination
from .errors import RelationViolationError
from .native_admission_rules import GoalRegistryAdmissionCheck, RegistryAdmissionCheck
from .registry_document import RegistrySnapshot
from .reservation_rules import ReservationViolationError
from .threads import Thread

if TYPE_CHECKING:
    from .registration import Registration


@dataclass(frozen=True, kw_only=True)
class RegistryOwner:
    check_type: ClassVar[type[RegistryAdmissionCheck]] = GoalRegistryAdmissionCheck
    thread: Thread
    admission_generation: int

    def require_active_turn(self):
        """Return the captured executable turn whose process/admission still agree."""
        turn = self.thread.active_turn
        if turn is None:
            raise StaleFence("registry owner has no active turn")
        if not self.thread.role.executable or not turn.owned_by(
            self.thread.pid, self.admission_generation
        ):
            raise StaleFence("registry owner turn witness is invalid")
        return turn

    @classmethod
    def capture(cls, snapshot: RegistrySnapshot, name: str, reason: str) -> RegistryOwner:
        thread = snapshot.threads.get(name)
        admission = snapshot.admission_generations.get(name)
        if thread is None or admission is None:
            raise StaleFence(reason)
        owner = cls(thread=thread, admission_generation=admission)
        owner.require_snapshot(snapshot, reason)
        return owner

    def _require_current(self, actual: Thread, admission: int | None, reason: str) -> None:
        try:
            self.check_type(
                expected=self.thread,
                actual=actual,
                expected_admission=self.admission_generation,
                admission=admission,
                process=ProcessIdentity.capture(os.getpid()),
            ).require_valid()
        except ReservationViolationError as error:
            raise StaleFence(f"{reason}: {error}") from error

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

    def coordinator_identity(self, lookup: str) -> OwnerGenerations:
        return OwnerGenerations(
            owner_lookup=lookup, owner_thread=self.thread.name, generation=self.generation
        )

    def require(self, store: Coordination, lookup: str) -> None:
        participant = store.participants.get(lookup)
        if (
            not participant.committed
            or participant.owner_thread != self.thread.name
            or participant.participant_generation != self.generation
            or stable_thread_lookup(self.thread.created_at) != lookup
            or self.thread.process_identity != ProcessIdentity.capture(os.getpid())
            or not self.thread.role.executable
        ):
            raise StaleFence("cohort recipient is not this live registered owner generation")
