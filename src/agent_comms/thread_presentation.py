"""Thread presentation: declaration and persistence owners."""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import TYPE_CHECKING

from .declared_family import DeclaredFamily
from .thread_identity import OwnerIdentity
from .child_process import ProcessIdentity

if TYPE_CHECKING:
    from .presentation import MessageNotification
    from .transcripts import TranscriptReadIdentity


class ThreadOwnerBinding(DeclaredFamily, affix="ThreadOwnerBinding"):
    """A projection of the registry lease, never an alternative owner store."""

    def replaces(self, original: LiveThreadOwnerBinding) -> bool:
        return False

    def superseded_by(self, replacement: ThreadOwnerBinding) -> bool:
        return False

    def recipient_activity(self, agents, snapshot, thread):
        from .agent_activity import UnavailableRecipientActivity

        return UnavailableRecipientActivity()

    def native_presentation(self, ordinary: ThreadPresentation) -> ThreadPresentation:
        """Display this acquired binding, without observing another process cut."""
        return replace(ordinary, marker="○", summary="Owner exited", busy=False)


@dataclass(frozen=True, slots=True)
class UnavailableThreadOwnerBinding(ThreadOwnerBinding):
    pass


@dataclass(frozen=True, slots=True)
class LiveThreadOwnerBinding(ThreadOwnerBinding):
    owner: OwnerIdentity
    process: ProcessIdentity

    def native_presentation(self, ordinary: ThreadPresentation) -> ThreadPresentation:
        return ordinary

    def recipient_activity(self, agents, snapshot, thread):
        from .agent_activity import LiveRecipientActivity

        return LiveRecipientActivity(thread, agents.activity_of(thread.name, snapshot=snapshot))

    def superseded_by(self, replacement: ThreadOwnerBinding) -> bool:
        return replacement.replaces(self)

    def replaces(self, original: LiveThreadOwnerBinding) -> bool:
        # Both operands are the ORIGINAL registry owner/process witnesses.
        # Queue admission generations are a different allocation domain.
        if self.owner.incarnation != original.owner.incarnation:
            return False
        if self.owner.generation < original.owner.generation:
            return False
        return self != original



@dataclass(frozen=True, slots=True)
class ThreadPresentation:
    title: str
    marker: str
    summary: str
    busy: bool = False
    notifications: tuple[MessageNotification, ...] = ()
    attention: bool = False
    binding: ThreadOwnerBinding = UnavailableThreadOwnerBinding()
    read_identity: TranscriptReadIdentity | None = None

    @property
    def label(self) -> str:
        return f"{self.marker} {self.title}"
