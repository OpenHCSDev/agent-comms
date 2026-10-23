"""Registration changes control the owner generation, updates and permission to start turns.

These are transient operations over the one RegistryDocument, never a second
registry or a stored event format. A replacement always has a previous owner;
a first registration cannot accidentally borrow one.
"""

from __future__ import annotations

import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING

from .errors import RelationViolationError
from .coordination_session import CoordinationSession
from .goals import GoalDeclaration, NoGoal
from .participant_store import ParticipantStore
from .thread_status import ThreadStatus
from .threads import Thread

if TYPE_CHECKING:
    from .registry_document import RegistryDocument


@dataclass(frozen=True, slots=True, kw_only=True)
class RegistrationChange(ABC):
    thread: Thread
    status: ThreadStatus

    @property
    @abstractmethod
    def prior_goal(self) -> GoalDeclaration: ...

    @property
    @abstractmethod
    def changes_owner(self) -> bool: ...

    @abstractmethod
    def restarted(self) -> RegistrationChange: ...

    def declared(self, requested: Thread) -> RegistrationChange:
        """Initial declarations have no prior executor to preserve or replace."""
        return self

    def prepare_participant(self, root: Path) -> None:
        """An update retains the participant its incarnation already declared."""

    def apply(self, document: RegistryDocument) -> None:
        """Caller holds registry/publication authority and records goal history."""
        thread = self.thread.restarted_tools() if self.changes_owner else self.thread
        document.install_thread(thread)
        document.set_status(thread.name, self.status)
        document.last_seen[thread.name] = time.time()
        if self.changes_owner:
            document.advance_claim(thread.name)
            document.owners.advance(thread.name)


class InitialRegistration(RegistrationChange):
    prior_goal = NoGoal()
    changes_owner = True

    def prepare_participant(self, root: Path) -> None:
        """The participant commits before the registry makes this incarnation visible."""
        with CoordinationSession(str(CoordinationSession.path_in(root))) as session:
            ParticipantStore(session).register(self.thread.incarnation.lookup, committed=True)

    def restarted(self) -> RegistrationChange:
        return InitialOwnerRegistration(thread=self.thread, status=self.status)


@dataclass(frozen=True, slots=True, kw_only=True)
class UpdatedRegistration(RegistrationChange):
    previous: Thread
    previous_status: ThreadStatus

    def declared(self, requested: Thread) -> RegistrationChange:
        """Decide whether to keep or claim execution against the locked prior owner."""
        if self.previous.executing:
            if requested.execution is not self.previous.execution:
                raise RelationViolationError("Cannot replace receiving capability during its active turn.")
            if requested.process_identity.conflicts_with(self.previous.process_identity):
                raise RelationViolationError("Cannot replace an executor during its active turn.")
            return replace(
                self,
                thread=replace(
                    self.thread,
                    process_identity=self.previous.process_identity,
                    turn_record=self.previous.turn_record,
                    turn_generation=self.previous.turn_generation,
                ),
            )
        return requested.process_identity.registration_change(
            self.previous.process_identity, self, requested.incarnation
        )

    @property
    def prior_goal(self) -> GoalDeclaration:
        return self.previous.goal

    @property
    def changes_owner(self) -> bool:
        return (
            self.previous.process_identity != self.thread.process_identity
            or self.previous.execution is not self.thread.execution
            or self.previous.role != self.thread.role
            or self.previous_status.changes_owner(self.status)
        )

    def restarted(self) -> RegistrationChange:
        return OwnerRestartRegistration(
            thread=self.thread,
            status=self.status,
            previous=self.previous,
            previous_status=self.previous_status,
        )


class NativeSourcePublication(UpdatedRegistration):
    """The leased owner keeps identity/heartbeat/goal; only its source is published.

    The existing identity-change property still requires the publication handoff
    fence when the actual canonical session changes.
    """

    def apply(self, document: RegistryDocument) -> None:
        document.install_thread(self.thread)


class ExplicitOwnerChange(RegistrationChange):
    """Fresh explicit registration always revokes imported permission to run a turn."""

    changes_owner = True

    def restarted(self) -> RegistrationChange:
        return self


class InitialOwnerRegistration(ExplicitOwnerChange, InitialRegistration):
    pass


class OwnerRestartRegistration(ExplicitOwnerChange, UpdatedRegistration):
    pass
