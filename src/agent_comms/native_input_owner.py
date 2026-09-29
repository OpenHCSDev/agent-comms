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
from .native_admission_rules import (
    GoalRegistryAdmissionCheck,
    GoalRegistryIdentityCheck,
    RegistryAdmissionCheck,
)
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

    @classmethod
    def capture_local(cls, snapshot: RegistrySnapshot, name: str) -> RegistryOwner:
        """Read local executable authority from this single locked snapshot."""
        thread = snapshot.require_active(name)
        thread.require_local_process(ProcessIdentity.capture(os.getpid()))
        admission = snapshot.admission_generations[thread.name]
        thread.require_current_turn(admission)
        return cls(thread=thread, admission_generation=admission)

    def require_exact(
        self, snapshot: RegistrySnapshot, expected: Thread, generation: int
    ) -> None:
        if snapshot.owner_identity(self.thread.name) != expected.owner_identity(generation):
            raise RelationViolationError("live owner generation changed")
        if self.thread != expected:
            raise RelationViolationError("live owner declaration changed")

    def require_claim(self, expected: Thread, admission: int) -> None:
        try:
            GoalRegistryIdentityCheck(
                expected=expected, actual=self.thread,
                expected_admission=admission, admission=self.admission_generation,
                process=ProcessIdentity.capture(os.getpid()),
            ).require_valid()
        except ReservationViolationError as error:
            raise RelationViolationError(f"live owner changed before turn claim: {error}") from error
        self.thread.require_idle()

    def require_active_turn(self):
        """Return the captured executable turn whose process/admission still agree."""
        turn = self.thread.active_turn
        if turn is None:
            raise StaleFence("registry owner has no active turn")
        try:
            self.thread.role.require_executable()
        except RelationViolationError as error:
            raise StaleFence("registry owner is not executable") from error
        if not turn.owned_by(self.thread.pid, self.admission_generation):
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
        try:
            actual = snapshot.require_active(self.thread.name)
        except RelationViolationError as error:
            raise StaleFence(reason) from error
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
        if not participant.committed or participant.owner_identity != self.coordinator_identity(lookup):
            raise StaleFence("cohort participant generation changed")
        if stable_thread_lookup(self.thread.created_at) != lookup:
            raise StaleFence("cohort participant incarnation changed")
        if self.thread.process_identity != ProcessIdentity.capture(os.getpid()):
            raise StaleFence("cohort recipient is not this live registered owner generation")
        try:
            self.thread.role.require_executable()
        except RelationViolationError as error:
            raise StaleFence("cohort participant is not executable") from error
