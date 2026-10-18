"""Thread presence, lifecycle decisions and their display/control projections."""

from __future__ import annotations

from abc import abstractmethod
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, ClassVar

from .child_process import ProcessState
from .declared_family import DeclaredFamily
from .errors import RelationViolationError
from .field_codec import FieldCodec, TextRepresentation
from .thread_presentation import ThreadOwnerBinding, ThreadPresentation

if TYPE_CHECKING:
    from .activity import ObservedActivity
    from .registry_document import RosterVisibility


@dataclass(frozen=True)
class ThreadStatus(DeclaredFamily, affix="ThreadStatus"):
    active: ClassVar[bool] = False
    running: ClassVar[bool] = False
    stopped: ClassVar[bool] = False
    visible: ClassVar[bool] = False

    @abstractmethod
    def in_view(self, visibility: RosterVisibility) -> bool:
        """Whether this lifecycle belongs in the requested executable roster."""

    def require_visible(self) -> None:
        if not self.visible:
            raise RelationViolationError("Initial direct target must be visible.")

    def require_running(self) -> None:
        raise RelationViolationError("goal owner is not running")

    def require_active(self) -> None:
        raise RelationViolationError("live owner is stopped or unavailable")

    def require_stopped(self) -> None:
        raise RelationViolationError("Retired owner is no longer stopped")

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
        return ThreadPresentation(title, "○", self.declared_name.title())


class ActiveThreadPresence:
    """Shared semantics of running and idle owners; not a second state family."""

    active = True
    visible = True

    def require_active(self) -> None:
        pass

    def in_view(self, visibility: RosterVisibility) -> bool:
        return True

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


class RunningThreadStatus(ActiveThreadPresence, ThreadStatus):
    running = True

    def require_running(self) -> None:
        pass

    def require_self_rename(self) -> None:
        pass


class IdleThreadStatus(ActiveThreadPresence, ThreadStatus):
    pass


class StoppedThreadStatus(ThreadStatus):
    stopped = True

    def require_stopped(self) -> None:
        pass
    visible = True

    def in_view(self, visibility: RosterVisibility) -> bool:
        return visibility.stopped


class CliInboxPresence:
    """Presence of a CLI inbox: only its bridge's registration changes it."""

    def restored(self) -> ThreadStatus:
        """A restored inbox has no bridge until one registers again."""
        return NoBridgeThreadStatus()

    def after_heartbeat(self, name: str) -> ThreadStatus:
        """A CLI command is not a bridge; a heartbeat keeps the inbox as it is."""
        self.require_mutable(name)
        return self


class ConnectedThreadStatus(CliInboxPresence, RunningThreadStatus):
    """A CLI inbox whose bridge process is watching it."""


class NoBridgeThreadStatus(CliInboxPresence, StoppedThreadStatus):
    """A CLI inbox with no bridge attached; its messages wait unread.

    It has no launched process to stop or restart, so it stays in every view.
    """

    def in_view(self, visibility: RosterVisibility) -> bool:
        return True

    def require_self_rename(self) -> None:
        """The CLI participant itself calls rename; it needs no bridge."""

    def presentation(self, title: str, activity: ObservedActivity) -> ThreadPresentation:
        return ThreadPresentation(title, "○", "CLI inbox · no bridge")


class ArchivedThreadStatus(ThreadStatus):
    def in_view(self, visibility: RosterVisibility) -> bool:
        return visibility.archived

    def restored(self) -> ThreadStatus:
        return self

    def allows_owner_control(self) -> bool:
        return False


class DeletingThreadStatus(ThreadStatus):
    def in_view(self, visibility: RosterVisibility) -> bool:
        return False

    def require_mutable(self, name: str) -> None:
        raise RelationViolationError(f"Thread {name!r} is being permanently deleted.")


class ThreadStatusName(TextRepresentation):
    @classmethod
    def encode(cls, value: object) -> object:
        return FieldCodec.encode(type(value))

    @classmethod
    def from_text(cls, value: str) -> ThreadStatus:
        return ThreadStatus.decode(value)()
