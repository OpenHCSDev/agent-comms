"""Thread presence, lifecycle decisions and their display/control projections."""

from __future__ import annotations

from abc import abstractmethod
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, ClassVar

from .child_process import ProcessState
from .declared_family import DeclaredFamily
from .errors import RelationViolationError, ThreadNotStoppedError
from .field_codec import FieldCodec, TextRepresentation
from .thread_presentation import ThreadOwnerBinding, ThreadPresentation, InactiveThreadPresentationStatus

if TYPE_CHECKING:
    from .activity import ObservedActivity
    from .registry_document import RosterVisibility
    from .threads import Thread


class AlwaysListed:
    """Presence listed independently of stopped/archived roster filters."""

    def in_view(self, visibility: RosterVisibility) -> bool:
        return True


class SelfRenaming:
    """The current participant may rename its own inbox."""

    def require_self_rename(self) -> None:
        pass


class Visible:
    """A presence that can be addressed, started and pinned in channels."""

    def require_visible(self, message: str = "Initial direct target must be visible.") -> None:
        pass


@dataclass(frozen=True)
class ThreadStatus(DeclaredFamily, affix="ThreadStatus"):
    active: ClassVar[bool] = False
    running: ClassVar[bool] = False
    stopped: ClassVar[bool] = False
    # Whether a message sent now is read now; each status answers, none by default.
    receiving: ClassVar[bool]

    @abstractmethod
    def in_view(self, visibility: RosterVisibility) -> bool:
        """Whether this lifecycle belongs in the requested executable roster."""

    def registered(self, thread: Thread) -> ThreadStatus:
        return self

    def declaration(self) -> ThreadStatus:
        return self

    def require_visible(self, message: str = "Initial direct target must be visible.") -> None:
        raise RelationViolationError(message)

    def require_running(self) -> None:
        raise RelationViolationError("goal owner is not running")

    def require_active(self) -> None:
        raise RelationViolationError("live owner is stopped or unavailable")

    def require_stopped(self) -> None:
        raise ThreadNotStoppedError("Retired owner is no longer stopped")

    def require_self_rename(self) -> None:
        """Only the thread's present participant may rename it."""
        raise RelationViolationError("Only a running thread can rename itself.")

    def require_mutable(self, name: str) -> None:
        """Registration/heartbeat may update this thread's presence."""

    def starts_owner(self, nxt: ThreadStatus) -> bool:
        return not self.active and nxt.active

    def changes_owner(self, nxt: ThreadStatus) -> bool:
        return self.active != nxt.active

    def restored(self) -> ThreadStatus:
        """Restoring saved provenance never restarts its original process."""
        return StoppedThreadStatus()

    def after_heartbeat(self, name: str) -> ThreadStatus:
        self.require_mutable(name)
        return RunningThreadStatus()

    def for_deletion(self) -> ThreadStatus:
        return DeletingThreadStatus()

    def allows_owner_control(self) -> bool:
        return True

    def allows_owner_start(self, *, process: ProcessState) -> bool:
        return self.allows_owner_control()

    def native_presentation(
        self, process: ProcessState, ordinary: ThreadPresentation, binding: ThreadOwnerBinding,
    ) -> ThreadPresentation:
        return ordinary

    def external_presentation(
        self, process: ProcessState, ordinary: ThreadPresentation,
    ) -> ThreadPresentation:
        return ordinary

    def presentation(self, title: str, activity: ObservedActivity) -> ThreadPresentation:
        return ThreadPresentation(title, "○", self.declared_name.title(),
                                  status=InactiveThreadPresentationStatus())


class ActiveThreadPresence(AlwaysListed, Visible, ThreadStatus, declared_name=None):
    """Shared semantics of running and idle owners; not a second state family."""

    active = True

    def require_active(self) -> None:
        pass

    def for_deletion(self) -> ThreadStatus:
        raise RelationViolationError("Stop a running thread before permanently deleting it.")

    def allows_owner_start(self, *, process: ProcessState) -> bool:
        return process.allows_owner_start()

    def native_presentation(
        self, process: ProcessState, ordinary: ThreadPresentation, binding: ThreadOwnerBinding,
    ) -> ThreadPresentation:
        return process.native_presentation(ordinary, binding)

    def external_presentation(
        self, process: ProcessState, ordinary: ThreadPresentation,
    ) -> ThreadPresentation:
        if process.offline():
            return replace(
                ordinary, marker="○", summary="CLI offline", status=ordinary.status.without_work(),
            )
        return ordinary

    def presentation(self, title: str, activity: ObservedActivity) -> ThreadPresentation:
        return activity.ordinary_presentation(title)


class RunningThreadStatus(SelfRenaming, ActiveThreadPresence):
    running = True
    receiving = True

    def require_running(self) -> None:
        pass


class IdleThreadStatus(ActiveThreadPresence):
    receiving = True


class StoppedThreadStatus(Visible, ThreadStatus):
    stopped = True
    receiving = False

    def require_stopped(self) -> None:
        pass

    def in_view(self, visibility: RosterVisibility) -> bool:
        return visibility.stopped


class CliInboxPresence(AlwaysListed):
    """The declared inbox resolves its bridge process at every read."""

    def registered(self, thread: Thread) -> ThreadStatus:
        return (ConnectedThreadStatus() if thread.process_identity.alive()
                else NoBridgeThreadStatus())

    def declaration(self) -> ThreadStatus:
        return NoBridgeThreadStatus()

    def restored(self) -> ThreadStatus:
        """Restoration preserves the inbox declaration, never a liveness answer."""
        return NoBridgeThreadStatus()

    def after_heartbeat(self, name: str) -> ThreadStatus:
        """A CLI command is not a bridge; a heartbeat keeps the inbox as it is."""
        self.require_mutable(name)
        return self


class ConnectedThreadStatus(CliInboxPresence, RunningThreadStatus):
    """A CLI inbox whose bridge process is watching it."""


class NoBridgeThreadStatus(SelfRenaming, CliInboxPresence, StoppedThreadStatus):
    """A CLI inbox with no bridge attached; its messages wait unread.

    It has no launched process to stop or restart, so it stays in every view.
    """

    def presentation(self, title: str, activity: ObservedActivity) -> ThreadPresentation:
        return replace(super().presentation(title, activity), summary="CLI inbox · no bridge")


class ArchivedThreadStatus(ThreadStatus):
    receiving = False

    def in_view(self, visibility: RosterVisibility) -> bool:
        return visibility.archived

    def restored(self) -> ThreadStatus:
        return self

    def allows_owner_control(self) -> bool:
        return False


class DeletingThreadStatus(ThreadStatus):
    receiving = False

    def in_view(self, visibility: RosterVisibility) -> bool:
        return False

    def require_mutable(self, name: str) -> None:
        raise RelationViolationError(f"Thread {name!r} is being permanently deleted.")


class ThreadStatusName(TextRepresentation[ThreadStatus]):
    @classmethod
    def encode(cls, value: object) -> object:
        return FieldCodec.encode(type(value))

    @classmethod
    def from_text(cls, value: str) -> ThreadStatus:
        return ThreadStatus.member_named(value)()
