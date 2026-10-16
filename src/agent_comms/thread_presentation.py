"""Thread presentation: declaration and persistence owners."""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import TYPE_CHECKING

from .declared_family import DeclaredFamily
from .thread_identity import OwnerIdentity
from .child_process import ProcessIdentity

if TYPE_CHECKING:
    from .presentation import MessageNotification
    from .registry_document import RegistrySnapshot
    from .transcripts import TranscriptReadIdentity, TranscriptSourceWindow


class ThreadPresentationStatus(DeclaredFamily, affix="ThreadPresentationStatus"):
    """Display activity and warnings, not registry presence or turn authority."""

    @property
    def busy(self) -> bool:
        return False

    @property
    def attention(self) -> bool:
        return False

    def with_attention(self) -> ThreadPresentationStatus:
        return AttentionThreadPresentationStatus(self)

    def without_work(self) -> ThreadPresentationStatus:
        return IdleThreadPresentationStatus()


@dataclass(frozen=True, slots=True)
class IdleThreadPresentationStatus(ThreadPresentationStatus):
    pass


@dataclass(frozen=True, slots=True)
class WorkingThreadPresentationStatus(ThreadPresentationStatus):
    @property
    def busy(self) -> bool:
        return True


@dataclass(frozen=True, slots=True)
class AttentionThreadPresentationStatus(ThreadPresentationStatus):
    activity: ThreadPresentationStatus

    @property
    def busy(self) -> bool:
        return self.activity.busy

    @property
    def attention(self) -> bool:
        return True

    def with_attention(self) -> ThreadPresentationStatus:
        return self

    def without_work(self) -> ThreadPresentationStatus:
        return replace(self, activity=self.activity.without_work())


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
        return replace(ordinary, marker="○", summary="Owner exited",
                       status=ordinary.status.without_work())


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
    status: ThreadPresentationStatus = IdleThreadPresentationStatus()
    notifications: tuple[MessageNotification, ...] = ()
    sent_notifications: tuple[MessageNotification, ...] = ()
    binding: ThreadOwnerBinding = UnavailableThreadOwnerBinding()
    read_identity: TranscriptReadIdentity | None = None

    def same_observation(self, other: ThreadPresentation | None) -> bool:
        """Equal view and turn evidence, not merely equal global store revisions.

        Keep the exact registered thread and binding used for turn settlement.
        The transcript owner decides whether its page changed; unrelated bus,
        read-mark and coordinator writes do not create another page publication.
        """
        if other is None or replace(self, read_identity=other.read_identity) != other:
            return False
        original, current = self.read_identity, other.read_identity
        if original is None or current is None:
            return original is current
        return original.thread == current.thread and original.same_snapshot(current)

    def with_sources(self, window: TranscriptSourceWindow, *, snapshot: RegistrySnapshot) -> ThreadPresentation:
        """Incoming and sent outcomes are views of one captured notification window."""
        from .bus_publication import stable_thread_lookup
        from .notification_assignment import NotificationAssignment
        from .presentation import MessageNotification

        transcripts = window.read.owner
        sources = tuple({source.message.reference: source
                         for source in (*window.sources, *window.sent_sources)}.values())
        projected = MessageNotification.delivery_window(
            transcripts.root, transcripts.registry, sources,
            NotificationAssignment.read(transcripts.root, sources),
            snapshot=snapshot,
            owner_lookup=stable_thread_lookup(window.read.identity.thread.created_at),
        )
        incoming, sent = (
            tuple(replace(notification, message=source.message)
                  for source in selected
                  for notification in projected[source.message.seq, source.message.message_id])
            for selected in (window.sources, window.sent_sources)
        )
        return replace(self, notifications=incoming, sent_notifications=sent,
                       read_identity=window.read.identity)

    @property
    def label(self) -> str:
        return f"{self.marker} {self.title}"
