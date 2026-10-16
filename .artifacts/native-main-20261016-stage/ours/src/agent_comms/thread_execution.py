"""Declared receiving contracts, independent of agent/human authorship."""

from __future__ import annotations

from dataclasses import replace
from abc import ABC, abstractmethod
from typing import TYPE_CHECKING

from .declared_family import DeclaredFamily
from .errors import RelationViolationError

if TYPE_CHECKING:
    from .threads import Thread
    from .registry_document import RegistrySnapshot
    from .thread_presentation import ThreadPresentation, ThreadOwnerBinding


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
    def native_command_available(cls, command, status, *, owner_pid: int) -> bool:
        return False

    @classmethod
    def restart_candidates(cls, thread: Thread, status):
        return ()

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
    def native_command_available(cls, command, status, *, owner_pid: int) -> bool:
        return command.available_for(status, owner_pid=owner_pid)

    @classmethod
    def restart_candidates(cls, thread: Thread, status):
        return (thread,) if thread.role.executable and status.active and thread.process_alive else ()

    @classmethod
    def prepare_conversation(cls, preparation: ConversationPreparation):
        return preparation.native()

    @classmethod
    def require_native(cls) -> None:
        pass

    @classmethod
    def owner_binding(cls, snapshot: RegistrySnapshot, thread: Thread):
        from .thread_presentation import LiveThreadOwnerBinding, UnavailableThreadOwnerBinding

        try:
            snapshot.require_active(thread.name)
            process = thread.require_process()
        except RelationViolationError:
            return UnavailableThreadOwnerBinding()
        if not process.alive():
            return UnavailableThreadOwnerBinding()
        return LiveThreadOwnerBinding(snapshot.owner_identity(thread.name), process)

    @classmethod
    def presentation(cls, thread: Thread, status, ordinary: ThreadPresentation,
                     binding: ThreadOwnerBinding) -> ThreadPresentation:
        if status.active and thread.has_process:
            return binding.native_presentation(ordinary)
        return ordinary

    @classmethod
    def observe_recipient(cls, agents, snapshot: RegistrySnapshot, thread: Thread):
        binding = cls.owner_binding(snapshot, thread)
        return binding.recipient_activity(agents, snapshot, thread)

    @classmethod
    def project_activity(cls, thread: Thread, activity):
        """A running native owner displays its current turn phase."""
        return thread.turn_state.project_activity(activity) if thread.has_process else activity


class ExternalThreadExecution(ThreadExecution):
    @classmethod
    def require_external(cls) -> None:
        pass

    @classmethod
    def presentation(cls, thread: Thread, status, ordinary: ThreadPresentation,
                     binding: ThreadOwnerBinding) -> ThreadPresentation:
        # The CLI reports its own activity; between turns it is a participant.
        if not status.active:
            return ordinary
        # A stale busy report cannot keep a known-dead CLI working.
        if thread.has_process and not thread.process_alive:
            return replace(ordinary, marker="○", summary="CLI offline",
                           status=ordinary.status.without_work())
        return ordinary.status.live_cli_presentation(ordinary)

    @classmethod
    def observe_recipient(cls, agents, snapshot: RegistrySnapshot, thread: Thread):
        from .agent_activity import ExternalRecipientActivity

        return ExternalRecipientActivity(thread.incarnation)
