"""Declared receiving contracts, independent of agent/human authorship."""

from __future__ import annotations

import time
from abc import ABC, abstractmethod
from typing import TYPE_CHECKING

from .child_process import ProcessState
from .declared_family import DeclaredFamily
from .errors import RelationViolationError
from .thread_status import ThreadStatus

if TYPE_CHECKING:
    from .registry_document import RegistrySnapshot
    from .thread_presentation import ThreadOwnerBinding, ThreadPresentation
    from .threads import Thread


class ThreadCommandAvailability:
    @classmethod
    def available_for(cls, status: ThreadStatus, *, process: ProcessState) -> bool:
        return True

    @classmethod
    def available_for_thread(cls, thread: Thread, status: ThreadStatus) -> bool:
        return cls.available_for(status, process=thread.process_identity)


class NativeOwnerCommand(ThreadCommandAvailability):
    @classmethod
    def available_for_thread(cls, thread: Thread, status: ThreadStatus) -> bool:
        return thread.execution.native_command_available(
            cls, status, process=thread.process_identity,
        )


class OwnerLifecycleControl(ThreadCommandAvailability):
    @classmethod
    def available_for(cls, status: ThreadStatus, *, process: ProcessState) -> bool:
        return status.allows_owner_control()


class StartOwnerControl(OwnerLifecycleControl):
    @classmethod
    def available_for(cls, status: ThreadStatus, *, process: ProcessState) -> bool:
        return status.allows_owner_start(process=process)


class ArchiveOwnerControl(OwnerLifecycleControl):
    @classmethod
    def available_for(cls, status: ThreadStatus, *, process: ProcessState) -> bool:
        return status.stopped


class ConversationPreparation(ABC):
    @abstractmethod
    def native(self): ...

    @abstractmethod
    def external(self): ...


class ThreadExecution(DeclaredFamily, affix="ThreadExecution"):
    """The registration owns transport intent; presence does not imply an RPC."""

    @classmethod
    def require_native(cls) -> None:
        raise RelationViolationError("This participant checks its CLI inbox; it has no native prompt transport.")

    @classmethod
    def require_external(cls) -> None:
        raise RelationViolationError("Only an external CLI can publish a bridge process.")

    @classmethod
    def prepare_conversation(cls, preparation: ConversationPreparation):
        return preparation.external()

    @classmethod
    def native_command_available(cls, command, status, *, process: ProcessState) -> bool:
        return False

    @classmethod
    def registered_status(cls, thread: Thread, requested: ThreadStatus) -> ThreadStatus:
        """The lifecycle a registration installs; Core launched this owner."""
        return requested

    @classmethod
    def require_commander_fork(cls) -> None:
        raise RelationViolationError('This thread is not a declared commander fork.')

    @classmethod
    def restart_candidates(cls, thread: Thread, status):
        return ()

    @classmethod
    def order_candidates(cls, thread: Thread) -> tuple[Thread, ...]:
        """The thread when it can hold an order: a turn executor whose work state is ready."""
        return tuple(candidate for executor in thread.role.executor(thread)
                     for candidate in thread.work_state.candidates(executor))

    @classmethod
    def owner_binding(cls, snapshot: RegistrySnapshot, thread: Thread):
        from .thread_presentation import UnavailableThreadOwnerBinding

        return UnavailableThreadOwnerBinding()

    @classmethod
    def presentation(cls, thread: Thread, status, ordinary: ThreadPresentation,
                     binding: ThreadOwnerBinding) -> ThreadPresentation:
        return ordinary

    @classmethod
    def observe_recipient(cls, agents, snapshot: RegistrySnapshot, thread: Thread):
        from .agent_activity import UnavailableRecipientActivity

        return UnavailableRecipientActivity()

    @classmethod
    def project_activity(cls, thread: Thread, activity):
        """External participants report their own activity events."""
        return activity


class NativeThreadExecution(ThreadExecution):
    @classmethod
    def native_command_available(cls, command, status, *, process: ProcessState) -> bool:
        return command.available_for(status, process=process)

    @classmethod
    def restart_candidates(cls, thread: Thread, status):
        if thread.process_identity.runs_here():
            return ()
        return status.restart_candidates(thread)

    @classmethod
    def prepare_conversation(cls, preparation: ConversationPreparation):
        return preparation.native()

    @classmethod
    def require_native(cls) -> None:
        pass

    @classmethod
    def owner_binding(cls, snapshot: RegistrySnapshot, thread: Thread):
        return snapshot.status(thread.name).native_owner_binding(snapshot, thread)

    @classmethod
    def presentation(cls, thread: Thread, status: ThreadStatus, ordinary: ThreadPresentation,
                     binding: ThreadOwnerBinding) -> ThreadPresentation:
        return status.native_presentation(thread.process_identity, ordinary, binding)

    @classmethod
    def observe_recipient(cls, agents, snapshot: RegistrySnapshot, thread: Thread):
        binding = cls.owner_binding(snapshot, thread)
        return binding.recipient_activity(agents, snapshot, thread)

    @classmethod
    def project_activity(cls, thread: Thread, activity):
        """A running native owner displays its current turn phase."""
        return thread.turn_record.read().project_activity(activity)


class ExternalThreadExecution(ThreadExecution):
    @classmethod
    def require_external(cls) -> None:
        pass

    @classmethod
    def registered_status(cls, thread: Thread, requested: ThreadStatus) -> ThreadStatus:
        return requested.external_registered(thread, time.time())

    @classmethod
    def presentation(cls, thread: Thread, status: ThreadStatus, ordinary: ThreadPresentation,
                     binding: ThreadOwnerBinding) -> ThreadPresentation:
        return status.external_presentation(thread.process_identity, ordinary)

    @classmethod
    def observe_recipient(cls, agents, snapshot: RegistrySnapshot, thread: Thread):
        from .agent_activity import ExternalRecipientActivity

        return ExternalRecipientActivity(thread.incarnation)


class CommanderForkExecution(ExternalThreadExecution):
    """A commander child uses its CLI inbox, not a native process or bridge."""

    @classmethod
    def registered_status(cls, thread: Thread, requested: ThreadStatus) -> ThreadStatus:
        return requested

    @classmethod
    def require_commander_fork(cls) -> None:
        pass

    @classmethod
    def order_candidates(cls, thread: Thread) -> tuple[Thread, ...]:
        """A fork works inside its commander's turn and never holds an order."""
        return ()
