"""Declared receiving contracts, independent of agent/human authorship."""

from __future__ import annotations

from abc import ABC, abstractmethod
from contextlib import closing
from pathlib import Path
from typing import TYPE_CHECKING

from .declared_family import DeclaredFamily
from .child_process import ProcessState
from .errors import RelationViolationError
from .recorded_time import EventTime, RecordedEventTime, UnrecordedEventTime
from .thread_status import NoBridgeThreadStatus, ThreadStatus

if TYPE_CHECKING:
    from .threads import Thread
    from .registry_document import RegistrySnapshot
    from .thread_presentation import ThreadPresentation, ThreadOwnerBinding


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
    @abstractmethod
    def last_tool_call(cls, comms, thread: Thread) -> EventTime:
        """The newest original tool-call time in this execution's saved history."""

    @classmethod
    @abstractmethod
    def tool_call_between(cls, comms, thread: Thread, since: float, through: float) -> bool:
        """Whether an original tool call met an order's first-call deadline."""

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
    def _tool_call_times(cls, thread: Thread):
        from .native_transcript import NativeTranscript

        if not thread.session_file:
            return
        try:
            with closing(NativeTranscript(Path(thread.session_file)).tail()) as entries:
                for entry in entries:
                    if any(message.assistant and message.tool_calls for message in entry.messages):
                        yield entry.timestamp.at.timestamp()
        except FileNotFoundError:
            return

    @classmethod
    def last_tool_call(cls, comms, thread: Thread) -> EventTime:
        with closing(cls._tool_call_times(thread)) as times:
            return next((RecordedEventTime(timestamp) for timestamp in times), UnrecordedEventTime())

    @classmethod
    def tool_call_between(cls, comms, thread: Thread, since: float, through: float) -> bool:
        with closing(cls._tool_call_times(thread)) as times:
            return any(since <= timestamp <= through for timestamp in times)

    @classmethod
    def native_command_available(cls, command, status, *, process: ProcessState) -> bool:
        return command.available_for(status, process=process)

    @classmethod
    def restart_candidates(cls, thread: Thread, status):
        if thread.process_identity.runs_here():
            return ()
        return thread.role.executor(thread) if status.active and thread.process_alive else ()

    @classmethod
    def prepare_conversation(cls, preparation: ConversationPreparation):
        return preparation.native()

    @classmethod
    def require_native(cls) -> None:
        pass

    @classmethod
    def owner_binding(cls, snapshot: RegistrySnapshot, thread: Thread):
        from .thread_presentation import LiveThreadOwnerBinding, UnavailableThreadOwnerBinding

        if not snapshot.status(thread.name).active or not thread.process_alive:
            return UnavailableThreadOwnerBinding()
        return LiveThreadOwnerBinding(
            snapshot.owner_identity(thread.name), thread.require_process(),
        )

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
    def last_tool_call(cls, comms, thread: Thread) -> EventTime:
        from .external_transcript import ExternalTranscript

        return ExternalTranscript(comms.root, thread.incarnation).last_tool_call()

    @classmethod
    def tool_call_between(cls, comms, thread: Thread, since: float, through: float) -> bool:
        from .external_transcript import ExternalTranscript

        return ExternalTranscript(comms.root, thread.incarnation).tool_call_between(since, through)

    @classmethod
    def require_external(cls) -> None:
        pass

    @classmethod
    def registered_status(cls, thread: Thread, requested: ThreadStatus) -> ThreadStatus:
        return NoBridgeThreadStatus().registered(thread)

    @classmethod
    def presentation(cls, thread: Thread, status: ThreadStatus, ordinary: ThreadPresentation,
                     binding: ThreadOwnerBinding) -> ThreadPresentation:
        return status.external_presentation(thread.process_identity, ordinary)

    @classmethod
    def observe_recipient(cls, agents, snapshot: RegistrySnapshot, thread: Thread):
        from .agent_activity import ExternalRecipientActivity

        return ExternalRecipientActivity(thread.incarnation)
