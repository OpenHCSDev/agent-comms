"""Thread presentation: declaration and persistence owners."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from .declared_family import DeclaredFamily
from .thread_identity import OwnerIdentity, ThreadIncarnation
from .child_process import ProcessIdentity

if TYPE_CHECKING:
    from .presentation import MessageNotification


class ThreadOwnerBinding(DeclaredFamily, affix="ThreadOwnerBinding"):
    """A projection of the registry lease, never an alternative owner store."""

    def replaces(self, incarnation: ThreadIncarnation, owner_pid: int) -> bool:
        return False


@dataclass(frozen=True, slots=True)
class UnavailableThreadOwnerBinding(ThreadOwnerBinding):
    pass


@dataclass(frozen=True, slots=True)
class LiveThreadOwnerBinding(ThreadOwnerBinding):
    owner: OwnerIdentity
    process: ProcessIdentity

    def replaces(self, incarnation: ThreadIncarnation, owner_pid: int) -> bool:
        # Earlier coordination attests a thread incarnation and owner PID.
        # A different process for that same thread permits read-only reattach;
        # matching PIDs never infer a replacement from display text or timing.
        return self.owner.incarnation == incarnation and self.process.pid != owner_pid


@dataclass(frozen=True, slots=True)
class ThreadPresentation:
    title: str
    marker: str
    summary: str
    busy: bool = False
    notifications: tuple[MessageNotification, ...] = ()
    attention: bool = False
    binding: ThreadOwnerBinding = UnavailableThreadOwnerBinding()

    @property
    def label(self) -> str:
        return f"{self.marker} {self.title}"
