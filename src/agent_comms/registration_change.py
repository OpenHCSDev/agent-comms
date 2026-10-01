"""Registration transitions own epoch, publication and turn-admission consequences.

These are transient operations over the one RegistryDocument, never a second
registry or a stored event format. A replacement always has a previous owner;
a first registration cannot accidentally borrow one.
"""

from __future__ import annotations

import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING

from .errors import RelationViolationError
from .goals import Goal
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
    def prior_goal(self) -> Goal | None: ...

    @property
    @abstractmethod
    def needs_maintenance_admission(self) -> bool: ...

    @property
    @abstractmethod
    def changes_identity(self) -> bool: ...

    @property
    @abstractmethod
    def changes_owner(self) -> bool: ...

    @property
    def installed_thread(self) -> Thread:
        return self.thread.without_turn_admission()

    @abstractmethod
    def restarted(self) -> RegistrationChange: ...

    def declared(self, requested: Thread) -> RegistrationChange:
        """Initial declarations have no prior executor to preserve or replace."""
        return self

    def apply(self, document: RegistryDocument) -> None:
        """Caller holds registry/publication authority and records goal history."""
        thread = self.installed_thread
        document.threads[thread.name] = thread
        document.statuses[thread.name] = self.status
        document.last_seen[thread.name] = time.time()
        if self.changes_owner:
            document.admissions.advance(thread.name)
            document.owners.advance(thread.name)


class InitialRegistration(RegistrationChange):
    prior_goal = None
    changes_identity = False
    changes_owner = True

    @property
    def needs_maintenance_admission(self) -> bool:
        return self.thread.role.executable and self.thread.has_process and self.status.active

    def restarted(self) -> RegistrationChange:
        return InitialOwnerRegistration(thread=self.thread, status=self.status)


@dataclass(frozen=True, slots=True, kw_only=True)
class UpdatedRegistration(RegistrationChange):
    previous: Thread
    previous_status: ThreadStatus

    def declared(self, requested: Thread) -> RegistrationChange:
        """Decide executor preservation/admission against the locked prior owner."""
        if self.previous.executing:
            if requested.execution is not self.previous.execution:
                raise RelationViolationError("Cannot replace receiving capability during its active turn.")
            if requested.process_identity not in {None, self.previous.process_identity}:
                raise RelationViolationError("Cannot replace an executor during its active turn.")
            return replace(
                self,
                thread=replace(
                    self.thread,
                    process_identity=self.previous.process_identity,
                    active_turn=self.previous.active_turn,
                    turn_generation=self.previous.turn_generation,
                    last_finished_turn_id=self.previous.last_finished_turn_id,
                ),
            )
        if (
            self.previous.has_process
            and requested.has_process
            and requested.incarnation != self.previous.incarnation
        ):
            return self.restarted()
        return self

    @property
    def prior_goal(self) -> Goal | None:
        return self.previous.goal

    @property
    def needs_maintenance_admission(self) -> bool:
        return self.thread.role.executable and (
            self.previous.process_identity != self.thread.process_identity
            or self.previous_status.starts_owner(self.status)
        )

    @property
    def changes_identity(self) -> bool:
        return (
            self.previous.publication_identity != self.thread.publication_identity
            or self.previous_status.changes_owner(self.status)
        )

    @property
    def changes_owner(self) -> bool:
        return (
            self.previous.process_identity != self.thread.process_identity
            or self.previous.execution is not self.thread.execution
            or self.previous.role != self.thread.role
            or self.previous_status.changes_owner(self.status)
        )

    @property
    def installed_thread(self) -> Thread:
        if self.thread.active_turn == self.previous.active_turn:
            return self.thread
        return self.thread.without_turn_admission()

    def restarted(self) -> RegistrationChange:
        return OwnerRestartRegistration(
            thread=self.thread,
            status=self.status,
            previous=self.previous,
            previous_status=self.previous_status,
        )


class ExplicitOwnerChange(RegistrationChange):
    """Fresh explicit admission always revokes imported turn authority."""

    changes_owner = True

    @property
    def needs_maintenance_admission(self) -> bool:
        return self.thread.role.executable

    @property
    def installed_thread(self) -> Thread:
        return self.thread.without_turn_admission()

    def restarted(self) -> RegistrationChange:
        return self


class InitialOwnerRegistration(ExplicitOwnerChange, InitialRegistration):
    pass


class OwnerRestartRegistration(ExplicitOwnerChange, UpdatedRegistration):
    changes_identity = True
