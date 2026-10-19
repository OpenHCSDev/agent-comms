"""Thread presentation: declaration and persistence owners."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, ClassVar

from .errors import RelationViolationError

from .declared_family import DeclaredFamily
from .thread_identity import OwnerIdentity
from .child_process import ProcessIdentity

if TYPE_CHECKING:
    from .message_reference import MessageReference
    from .presentation import MessageNotification
    from .transcripts import TranscriptReadIdentity, TranscriptSourceWindow


@dataclass(frozen=True, kw_only=True)
class ThreadPresentationStatus(DeclaredFamily, affix="ThreadPresentationStatus"):
    """Display activity and warnings, not registry presence or turn authority."""

    working: ClassVar[bool] = False
    details: tuple[str, ...] = ()

    def when_idle(self, ordinary: ThreadPresentation,
                  present: Callable[[ThreadPresentation], ThreadPresentation]) -> ThreadPresentation:
        """Compose idle-only explanations without replacing genuine work."""
        if self.working:
            return ordinary
        shown = present(replace(ordinary, status=replace(self, details=())))
        return shown.status.with_attention(*self.details).present_attention(shown)

    def present_attention(self, ordinary: ThreadPresentation) -> ThreadPresentation:
        """Warnings change idle paint, never the summary of real work."""
        if self.details and not self.working:
            ordinary = replace(ordinary, marker="!", summary=self.details[0])
        return replace(ordinary, status=self)

    def activity_presentation(self, title: str, activity: str, detail: str) -> ThreadPresentation:
        return self.present_attention(ThreadPresentation(title, "✓", "Ready", status=self))

    def require_activity_detail(self, detail: str) -> None:
        if detail and not self.working:
            raise RelationViolationError("Idle activity cannot carry a detail.")

    def with_attention(self, *details: str) -> ThreadPresentationStatus:
        return replace(self, details=(*self.details, *details))

    def without_work(self) -> ThreadPresentationStatus:
        return IdleThreadPresentationStatus(details=self.details)

    def with_disconnection[T](self, ordinary: T, disconnected: Callable[[], T]) -> T:
        """A failed viewer connection normally supplies the displayed state."""
        return disconnected()


@dataclass(frozen=True, slots=True)
class IdleThreadPresentationStatus(ThreadPresentationStatus):
    pass


@dataclass(frozen=True, slots=True)
class InactiveThreadPresentationStatus(IdleThreadPresentationStatus):
    """An inactive owner's state is not replaced by a failed viewer connection."""

    def with_disconnection[T](self, ordinary: T, disconnected: Callable[[], T]) -> T:
        return ordinary

    def without_work(self) -> ThreadPresentationStatus:
        return self


@dataclass(frozen=True, slots=True)
class WorkingThreadPresentationStatus(ThreadPresentationStatus):
    working: ClassVar[bool] = True

    def activity_presentation(self, title: str, activity: str, detail: str) -> ThreadPresentation:
        summary = activity.title()
        if detail := " ".join(detail.split()):
            summary += f" · {detail}"
        return ThreadPresentation(title, "⌛", summary, status=self)


@dataclass(frozen=True, slots=True)
class StatusPending(ThreadPresentationStatus):
    """The view opened before its first thread observation."""

    def presentation(self, title: str) -> ThreadPresentation:
        return ThreadPresentation(title, "…", "Waiting for agent status", status=self)


@dataclass(frozen=True, slots=True)
class SessionDisconnected(ThreadPresentationStatus):
    """A viewer's session to the thread failed; the owner process may still be live.

    Every place a viewer shows the thread (its row, tab, status line and
    input notice) shows this one state while that session stays failed.
    """

    reason: str

    @property
    def notice(self) -> str:
        return f"Thread disconnected: {self.reason}"

    def presentation(self, title: str) -> ThreadPresentation:
        return ThreadPresentation(title, "✗", self.notice, status=self)

    def over(self, ordinary: ThreadPresentation) -> ThreadPresentation:
        """The ordinary presentation, with its notifications, showing the disconnection."""
        shown = self.presentation(ordinary.title)
        return replace(ordinary, marker=shown.marker, summary=shown.summary, status=self)


@dataclass(frozen=True, slots=True)
class ThreadRetired(ThreadPresentationStatus):
    """An observation explicitly reports that the thread is gone."""

    def presentation(self, title: str) -> ThreadPresentation:
        return ThreadPresentation(title, "○", "Thread is no longer available", status=self)


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
        # Queued-input generations use a different counter.
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
    incoming_references: tuple[MessageReference, ...] = ()
    sent_references: tuple[MessageReference, ...] = ()

    def notification_activity_names(self) -> frozenset[str]:
        return frozenset(name for item in (*self.notifications, *self.sent_notifications)
                         for name in item.activity_names())

    def same_status(self, other: ThreadPresentation | None) -> bool:
        """Status changes do not require reacquiring an unchanged source window."""
        return other is not None and (self.title, self.marker, self.summary, self.status, self.binding) == (
            other.title, other.marker, other.summary, other.status, other.binding,
        )

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

    def with_sources(self, window: TranscriptSourceWindow) -> ThreadPresentation:
        """Incoming and sent outcomes are views of one captured notification window."""
        from .notification_assignment import NotificationAssignment
        from .presentation import MessageNotification, OwnerOutcomes

        transcripts = window.read.owner
        sources = tuple({source.message.reference: source
                         for source in (*window.sources, *window.sent_sources)}.values())
        projected = MessageNotification.delivery_window(
            transcripts.root, transcripts.registry, sources,
            NotificationAssignment.read(transcripts.root, sources),
            viewer=OwnerOutcomes.of(window.read.identity.thread),
        )
        incoming, sent = (
            tuple(replace(notification, message=source.message)
                  for source in selected
                  for notification in projected[source.message.seq, source.message.message_id])
            for selected in (window.sources, window.sent_sources)
        )
        return replace(
            self, notifications=incoming, sent_notifications=sent,
            read_identity=window.read.identity,
            incoming_references=tuple(source.message.reference for source in window.sources),
            sent_references=tuple(source.message.reference for source in window.sent_sources),
        )

    @property
    def label(self) -> str:
        return f"{self.marker} {self.title}"
