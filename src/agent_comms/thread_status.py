"""Thread presence, lifecycle decisions and their display/control projections."""

from __future__ import annotations

from abc import abstractmethod
from dataclasses import dataclass
from typing import TYPE_CHECKING, ClassVar

from .declared_family import DeclaredFamily

if TYPE_CHECKING:
    from .activity import Activity
    from .thread_presentation import ThreadPresentation


@dataclass(frozen=True)
class ThreadStatus(DeclaredFamily, affix="ThreadStatus"):
    active: ClassVar[bool] = False
    running: ClassVar[bool] = False
    stopped: ClassVar[bool] = False
    visible: ClassVar[bool] = False

    @abstractmethod
    def in_view(self, *, show_stopped: bool = True, show_archived: bool = False) -> bool:
        """Whether this lifecycle belongs in the requested executable roster."""

    def require_running(self) -> None:
        from .errors import RelationViolationError

        raise RelationViolationError("goal owner is not running")

    def require_active(self) -> None:
        from .errors import RelationViolationError

        raise RelationViolationError("live owner is stopped or unavailable")

    def require_stopped(self) -> None:
        from .errors import RelationViolationError

        raise RelationViolationError("Retired owner is no longer stopped")

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

    def allows_owner_start(self, *, owner_pid: int) -> bool:
        return self.allows_owner_control()

    def presentation(self, title: str, activity: Activity) -> ThreadPresentation:
        from .thread_presentation import ThreadPresentation

        return ThreadPresentation(title, "○", self.declared_name.title())


class ActiveThreadPresence:
    """Shared semantics of running and idle owners; not a second state family."""

    active = True
    visible = True

    def require_active(self) -> None:
        pass

    def in_view(self, *, show_stopped: bool = True, show_archived: bool = False) -> bool:
        return True

    def for_deletion(self) -> ThreadStatus:
        from .errors import RelationViolationError

        raise RelationViolationError("Stop a running thread before permanently deleting it.")

    def allows_owner_start(self, *, owner_pid: int) -> bool:
        return owner_pid <= 0

    def presentation(self, title: str, activity: Activity) -> ThreadPresentation:
        return activity.presentation(title)


class RunningThreadStatus(ActiveThreadPresence, ThreadStatus):
    running = True

    def require_running(self) -> None:
        pass


class IdleThreadStatus(ActiveThreadPresence, ThreadStatus):
    pass


class StoppedThreadStatus(ThreadStatus):
    stopped = True

    def require_stopped(self) -> None:
        pass
    visible = True

    def in_view(self, *, show_stopped: bool = True, show_archived: bool = False) -> bool:
        return show_stopped


class ArchivedThreadStatus(ThreadStatus):
    def in_view(self, *, show_stopped: bool = True, show_archived: bool = False) -> bool:
        return show_archived

    def restored(self) -> ThreadStatus:
        return self

    def allows_owner_control(self) -> bool:
        return False


class DeletingThreadStatus(ThreadStatus):
    def in_view(self, *, show_stopped: bool = True, show_archived: bool = False) -> bool:
        return False

    def require_mutable(self, name: str) -> None:
        from .errors import RelationViolationError

        raise RelationViolationError(f"Thread {name!r} is being permanently deleted.")
