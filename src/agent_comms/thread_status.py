"""Thread presence, lifecycle decisions and their display/control projections."""

from __future__ import annotations

from abc import abstractmethod
from dataclasses import dataclass
from typing import TYPE_CHECKING, ClassVar

from .declared_family import DeclaredFamily

if TYPE_CHECKING:
    from .declarations import Activity, ThreadPresentation


@dataclass(frozen=True)
class ThreadStatus(DeclaredFamily, affix="ThreadStatus"):
    active: ClassVar[bool] = False
    running: ClassVar[bool] = False
    stopped: ClassVar[bool] = False
    visible: ClassVar[bool] = False

    @abstractmethod
    def in_view(self, *, show_stopped: bool = True, show_archived: bool = False) -> bool:
        """Whether this lifecycle belongs in the requested executable roster."""

    def require_mutable(self, name: str) -> None:
        """Registration/heartbeat may update this thread's presence."""

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

    def allows_control(self, tool: str, *, owner_pid: int) -> bool:
        return True

    def presentation(self, title: str, activity: Activity) -> ThreadPresentation:
        from .declarations import ThreadPresentation

        return ThreadPresentation(title, "○", self.declared_name.title())


class ActiveThreadPresence:
    """Shared semantics of running and idle owners; not a second state family."""

    active = True
    visible = True

    def in_view(self, *, show_stopped: bool = True, show_archived: bool = False) -> bool:
        return True

    def for_deletion(self) -> ThreadStatus:
        from .declarations import RelationViolationError

        raise RelationViolationError("Stop a running thread before permanently deleting it.")

    def allows_control(self, tool: str, *, owner_pid: int) -> bool:
        return tool != "comms_start" or owner_pid <= 0

    def presentation(self, title: str, activity: Activity) -> ThreadPresentation:
        return activity.state.presentation(title, activity.detail)


class RunningThreadStatus(ActiveThreadPresence, ThreadStatus):
    running = True


class IdleThreadStatus(ActiveThreadPresence, ThreadStatus):
    pass


class StoppedThreadStatus(ThreadStatus):
    stopped = True
    visible = True

    def in_view(self, *, show_stopped: bool = True, show_archived: bool = False) -> bool:
        return show_stopped


class ArchivedThreadStatus(ThreadStatus):
    def in_view(self, *, show_stopped: bool = True, show_archived: bool = False) -> bool:
        return show_archived

    def restored(self) -> ThreadStatus:
        return self

    def allows_control(self, tool: str, *, owner_pid: int) -> bool:
        return tool not in {"comms_start", "comms_stop", "comms_archive"}


class DeletingThreadStatus(ThreadStatus):
    def in_view(self, *, show_stopped: bool = True, show_archived: bool = False) -> bool:
        return False

    def require_mutable(self, name: str) -> None:
        from .declarations import RelationViolationError

        raise RelationViolationError(f"Thread {name!r} is being permanently deleted.")
