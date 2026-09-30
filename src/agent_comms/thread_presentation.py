"""Thread presentation: declaration and persistence owners."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from .declared_family import DeclaredFamily
from .thread_identity import OwnerIdentity
from .child_process import ProcessIdentity

if TYPE_CHECKING:
    from .presentation import MessageNotification


class ThreadOwnerBinding(DeclaredFamily, affix="ThreadOwnerBinding"):
    """A projection of the registry lease, never an alternative owner store."""

    def replaces(self, owner: OwnerIdentity | None) -> bool:
        return False


@dataclass(frozen=True, slots=True)
class UnavailableThreadOwnerBinding(ThreadOwnerBinding):
    pass


@dataclass(frozen=True, slots=True)
class LiveThreadOwnerBinding(ThreadOwnerBinding):
    owner: OwnerIdentity
    process: ProcessIdentity

    def replaces(self, owner: OwnerIdentity | None) -> bool:
        # The attachment's original owner lease survives PID reuse. Only a
        # later registry generation for the same thread permits read-only
        # reattachment; process birth is revalidated at load admission.
        return (owner is not None and self.owner.incarnation == owner.incarnation
                and self.owner.generation > owner.generation)


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
