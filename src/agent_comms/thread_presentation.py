"""Thread presentation: declaration and persistence owners."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING

from .declared_family import DeclaredFamily
from .thread_identity import OwnerIdentity
from .child_process import ProcessIdentity

if TYPE_CHECKING:
    from .presentation import MessageNotification
    from .transcripts import TranscriptReadIdentity, TranscriptSourceWindow


class ThreadPresentationStatus(DeclaredFamily, affix="ThreadPresentationStatus"):
    """Display activity and warnings, not registry presence or turn authority."""

    @property
    def busy(self) -> bool:
        return bool(self.working_members(self))

    def working_members[T](self, member: T) -> tuple[T, ...]:
        """Select a participant only while this status carries work."""
        return ()

    def when_idle(self, ordinary: ThreadPresentation,
                  present: Callable[[ThreadPresentation], ThreadPresentation]) -> ThreadPresentation:
        """Compose idle-only explanations without replacing genuine work."""
        return present(ordinary)

    def activity_presentation(self, title: str, activity: str, detail: str) -> ThreadPresentation:
        return ThreadPresentation(title, "✓", "Ready", status=self)

    def require_activity_detail(self, detail: str) -> None:
        if detail:
            from .errors import RelationViolationError
            raise RelationViolationError("Idle activity cannot carry a detail.")

    def with_attention(self) -> ThreadPresentationStatus:
        return AttentionThreadPresentationStatus(self)

    def without_work(self) -> ThreadPresentationStatus:
        return IdleThreadPresentationStatus()

    def live_cli_presentation(self, ordinary: ThreadPresentation) -> ThreadPresentation:
        """Work and warnings keep their own explanation while the CLI is alive."""
        return ordinary


@dataclass(frozen=True, slots=True)
class IdleThreadPresentationStatus(ThreadPresentationStatus):
    def live_cli_presentation(self, ordinary: ThreadPresentation) -> ThreadPresentation:
        return replace(ordinary, marker="@", summary="CLI participant")


@dataclass(frozen=True, slots=True)
class WorkingThreadPresentationStatus(ThreadPresentationStatus):
    def working_members[T](self, member: T) -> tuple[T, ...]:
        return (member,)

    def when_idle(self, ordinary: ThreadPresentation,
                  present: Callable[[ThreadPresentation], ThreadPresentation]) -> ThreadPresentation:
        return ordinary

    def activity_presentation(self, title: str, activity: str, detail: str) -> ThreadPresentation:
        summary = activity.title()
        if detail := " ".join(detail.split()):
            summary += f" · {detail}"
        return ThreadPresentation(title, "⌛", summary, status=self)

    def require_activity_detail(self, detail: str) -> None:
        pass


@dataclass(frozen=True, slots=True)
class AttentionThreadPresentationStatus(ThreadPresentationStatus):
    activity: ThreadPresentationStatus

    def working_members[T](self, member: T) -> tuple[T, ...]:
        return self.activity.working_members(member)

    def when_idle(self, ordinary: ThreadPresentation,
                  present: Callable[[ThreadPresentation], ThreadPresentation]) -> ThreadPresentation:
        projected = self.activity.when_idle(ordinary, present)
        return replace(projected, status=projected.status.with_attention())

    def with_attention(self) -> ThreadPresentationStatus:
        return self

    def without_work(self) -> ThreadPresentationStatus:
        return replace(self, activity=self.activity.without_work())


@dataclass(frozen=True, slots=True)
class StatusPending(ThreadPresentationStatus):
    """The view opened before its first thread observation."""

    def presentation(self, title: str) -> ThreadPresentation:
        return ThreadPresentation(title, "…", "Waiting for agent status", status=self)


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

    def with_sources(self, window: TranscriptSourceWindow) -> ThreadPresentation:
        """Incoming and sent outcomes are views of one captured notification window."""
        from .notification_assignment import NotificationAssignment
        from .presentation import MessageNotification

        transcripts = window.read.owner
        sources = tuple({source.message.reference: source
                         for source in (*window.sources, *window.sent_sources)}.values())
        projected = MessageNotification.delivery_window(
            transcripts.root, transcripts.registry, sources,
            NotificationAssignment.read(transcripts.root, sources),
            owner_lookup=window.read.identity.thread.incarnation.lookup,
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
