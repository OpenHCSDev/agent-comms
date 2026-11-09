from __future__ import annotations

import time
from abc import abstractmethod
from collections.abc import Callable
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, ClassVar, Self

from agent_comms.errors import RelationViolationError, ThreadNotStoppedError, ThreadNotVisibleError
from agent_comms.execution.child_process import (
    ProcessFence,
    ProcessIdentity,
    ProcessSighting,
    ProcessState,
)
from agent_comms.execution.turn_item import SourceTurn
from agent_comms.execution.turn_lease import (
    FinishedTurnFence,
    NoFinishedTurn,
    TurnCompletion,
    TurnLeaseFence,
)
from agent_comms.messaging.message_reference import MessageReference
from agent_comms.messaging.message_send_reads import PresenceMessageSender
from agent_comms.nominal.declared_family import DeclaredFamily
from agent_comms.nominal.field_codec import FieldCodec, TextRepresentation
from agent_comms.nominal.lifecycle_capability import LifecycleCapability
from agent_comms.registry.activity import ObservedActivity
from agent_comms.registry.presence_reads import PresenceEntry, PresenceSnapshot, PresenceThread
from agent_comms.registry.roster_visibility import RosterVisibility
from agent_comms.registry.thread_identity import GenerationCounter, OwnerIdentity
from agent_comms.registry.thread_presentation import (
    InactiveThreadPresentationStatus,
    LiveThreadOwnerBinding,
    ThreadOwnerBinding,
    ThreadPresentation,
    UnavailableThreadOwnerBinding,
)
from agent_comms.registry.thread_reads import ThreadOwnerStatus


class AlwaysListed:
    """Presence listed independently of stopped/archived roster filters."""

    def in_view(self, visibility: RosterVisibility) -> bool:
        return True


class SelfRenaming:
    """The current participant may rename its own inbox."""

    def require_self_rename(self) -> None:
        pass


class PublishingPresence:
    """Publication permission is independent of appearing as an executable colleague."""

    def require_publication(self) -> None:
        pass


class Visible(PublishingPresence):
    """A presence that can be addressed, started and pinned in channels."""

    def reply(
        self, messaging: PresenceMessageSender, sender: str, target: str, body: str, *,
        answers: tuple[MessageReference, ...], source_turn: SourceTurn,
    ) -> tuple[MessageReference, ...]:
        return (messaging.send(
            sender=sender, target=target, body=body,
            answers=answers, source_turn=source_turn,
        ).message.reference,)

    def allows_owner_control(self) -> bool:
        return True

    def require_visible(self, message: str = "Initial direct target must be visible.") -> None:
        pass

    def publish_heartbeat[Sent](self, publish: Callable[[], Sent], unsent: Sent) -> Sent:
        return publish()


@dataclass(frozen=True)
class ThreadStatus(
    LifecycleCapability,
    DeclaredFamily,
    ProcessFence['ThreadStatus'],
    ThreadOwnerStatus[PresenceSnapshot, PresenceThread],
    affix='ThreadStatus',
):
    may_be_active: ClassVar[bool] = False
    running: ClassVar[bool] = False
    stopped: ClassVar[bool] = False
    # Whether a message sent now is read now; each status answers, none by default.
    receiving: ClassVar[bool] = False
    # Whether the thread has left every new audience and presence; only an archive has.
    archived: ClassVar[bool] = False
    # Whether a role slot in this lifecycle may take a new occupant: no one holds it now.
    vacant: ClassVar[bool] = False
    # Whether this row's session still runs in its recorded process, keeping that host alive.
    holds_process: ClassVar[bool] = False

    def require_hold_consent(self, require_consent: Callable[[], None]) -> None:
        """A thread that can still act owns the work it holds: changing it needs its consent."""
        require_consent()

    @abstractmethod
    def in_view(self, visibility: RosterVisibility) -> bool:
        """Whether this lifecycle belongs in the requested executable roster."""

    def reply(
        self, messaging: PresenceMessageSender, sender: str, target: str, body: str, *,
        answers: tuple[MessageReference, ...], source_turn: SourceTurn,
    ) -> tuple[MessageReference, ...]:
        """Unavailable recipients retain their answer in the transcript, not a new DM."""
        return ()

    def registered(self, thread: PresenceThread, last_seen: float) -> ThreadStatus:
        return self

    def declaration(self) -> ThreadStatus:
        return self

    def unbound(self) -> ThreadStatus:
        return self

    def fenced(self) -> ThreadStatus:
        """This stored status while its process still binds it; only a stop has a fence."""
        return self

    def external_registered(self, thread: PresenceThread, now: float) -> ThreadStatus:
        return NoBridgeThreadStatus().registered(thread, now)

    def paused_since(self, last_seen: float) -> tuple[float, ...]:
        return ()

    def claimed_readers(self, name: str) -> tuple[str, ...]:
        """The inbox this status claims a reader is taking right now. Every other status
        already says it is not reading, so its inbox is never read to check."""
        return ()

    @property
    def answers_later(self) -> bool:
        """Whether a wait on this thread is answered once it reads; a running thread does."""
        return self.running

    def require_visible(self, message: str = "Initial direct target must be visible.") -> None:
        raise ThreadNotVisibleError(message)

    def publish_heartbeat[Sent](self, publish: Callable[[], Sent], unsent: Sent) -> Sent:
        """Core's heartbeat never messages an unaddressable thread (archived, deleting, a service):
        its board frame is still written. On 2026-11-02 a packet to such a lead, then a rebase
        reminder to such a branch holder, each failed every pass for the whole team."""
        return unsent

    def require_publication(self) -> None:
        raise RelationViolationError("Message sender is not permitted to publish.")

    def require_running(self) -> None:
        raise RelationViolationError("goal owner is not running")

    def require_active(self, message: str = "live owner is stopped or unavailable") -> None:
        self.require(ActiveThreadPresence, RelationViolationError(message))

    def require_inactive(self, message: str) -> None:
        """Inactive declarations may be changed without an active owner's session."""

    def require_stopped(self) -> None:
        raise ThreadNotStoppedError("Retired owner is no longer stopped")

    def require_self_rename(self) -> None:
        """Only the thread's present participant may rename it."""
        raise RelationViolationError("Only a running thread can rename itself.")

    def require_mutable(self, name: str) -> None:
        """Registration/heartbeat may update this thread's presence."""

    def withdraw_owner(self, counter: GenerationCounter, name: str) -> None:
        """Withdraw this presence without inventing an owner it did not hold."""

    def starts_owner(self, nxt: ThreadStatus) -> bool:
        return bool(nxt.items_with(ActiveThreadPresence, nxt))

    def changes_owner(self, nxt: ThreadStatus) -> bool:
        return self.starts_owner(nxt) or nxt.starts_owner(self)

    def native_owner_binding(self, snapshot: PresenceSnapshot, thread: PresenceThread) -> ThreadOwnerBinding:
        return UnavailableThreadOwnerBinding()

    def restart_candidates(self, thread: PresenceThread) -> tuple[PresenceThread, ...]:
        return ()

    def deletable_threads(self, thread: PresenceThread) -> tuple[PresenceThread, ...]:
        return () if thread.process_alive else (thread,)

    def owns_entry(self, entry: PresenceEntry, owner: OwnerIdentity, process: ProcessIdentity) -> bool:
        return False

    def completed_turn(self, lease: TurnLeaseFence) -> TurnCompletion:
        return NoFinishedTurn()

    def restored(self) -> ThreadStatus:
        """Restoring saved provenance never restarts its original process."""
        return StoppedThreadStatus()

    def after_heartbeat(self, name: str) -> ThreadStatus:
        self.require_mutable(name)
        return ProcessThreadStatus()

    def require_deleting(self) -> None:
        raise RelationViolationError("Thread has not entered permanent deletion.")

    def for_deletion(self) -> ThreadStatus:
        return DeletingThreadStatus()

    def allows_owner_control(self) -> bool:
        return False

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

    def presentation(
        self, title: str, activity: ObservedActivity,
        idle: Callable[[ThreadPresentation], ThreadPresentation],
    ) -> ThreadPresentation:
        return ThreadPresentation(title, "○", self.declared_name.title(),
                                  status=InactiveThreadPresentationStatus())


class HostBoundPresence:
    """A session still running in the recorded process: it keeps that host alive, and a
    restart retires and relaunches it while the process lives."""

    holds_process: ClassVar[bool] = True

    def restart_candidates(self, thread: PresenceThread) -> tuple[PresenceThread, ...]:
        return thread.role.executor(thread) if thread.process_alive else ()


class ActiveThreadPresence(
    HostBoundPresence, AlwaysListed, Visible, ThreadStatus, ProcessSighting[ThreadStatus],
    declared_name=None,
):
    """Shared semantics of running and idle owners; not a second state family."""

    receiving = True
    may_be_active = True

    def registered(self, thread: PresenceThread, last_seen: float) -> ThreadStatus:
        return thread.process_identity.thread_status(self, last_seen)

    def process_seen(self, process: ProcessState) -> ThreadStatus:
        return self if process.alive() else StoppedThreadStatus()

    def heartbeat_seen(self, last_seen: float) -> ThreadStatus:
        return HeartbeatThreadStatus().when_seen(last_seen)

    def require_inactive(self, message: str) -> None:
        raise RelationViolationError(message)

    def starts_owner(self, nxt: ThreadStatus) -> bool:
        return False

    def withdraw_owner(self, counter: GenerationCounter, name: str) -> None:
        counter.advance(name)

    def native_owner_binding(self, snapshot: PresenceSnapshot, thread: PresenceThread) -> ThreadOwnerBinding:
        if not thread.process_alive:
            return super().native_owner_binding(snapshot, thread)
        return LiveThreadOwnerBinding(snapshot.owner_identity(thread.name), thread.require_process())

    def deletable_threads(self, thread: PresenceThread) -> tuple[PresenceThread, ...]:
        return ()

    def owns_entry(self, entry: PresenceEntry, owner: OwnerIdentity, process: ProcessIdentity) -> bool:
        return (entry.matches_name(owner.incarnation.name)
                and owner.owns(entry.thread, entry.owner_generation, process))

    def completed_turn(self, lease: TurnLeaseFence) -> TurnCompletion:
        return FinishedTurnFence(
            identity=lease.identity, turn_id=lease.turn_id,
            claimed_generation=lease.claimed_generation,
        )

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

    def presentation(
        self, title: str, activity: ObservedActivity,
        idle: Callable[[ThreadPresentation], ThreadPresentation],
    ) -> ThreadPresentation:
        ordinary = activity.ordinary_presentation(title)
        ordinary = ordinary.status.when_idle(ordinary, idle)
        return activity.readiness.presentation(ordinary)


class ProcessThreadStatus(ActiveThreadPresence):
    """A registration awaiting observation, not a saved liveness answer."""

    def process_seen(self, process: ProcessState) -> ThreadStatus:
        return RunningThreadStatus().process_seen(process)


class RunningThreadStatus(SelfRenaming, ActiveThreadPresence):
    running = True

    def declaration(self) -> ThreadStatus:
        return ProcessThreadStatus()

    def require_running(self) -> None:
        pass


class HeartbeatThreadStatus(RunningThreadStatus):
    """A processless participant whose presence expires without another heartbeat."""

    max_age: ClassVar[float] = 120.0

    def when_seen(self, last_seen: float) -> ThreadStatus:
        return self if time.time() - last_seen <= self.max_age else StoppedThreadStatus()


class IdleThreadStatus(ActiveThreadPresence):
    pass


class OrphansHolds:
    """A holder that can never answer: the work it held is orphaned, and any teammate may
    release or withdraw it, as the REHOME line tells them to."""

    def require_hold_consent(self, require_consent: Callable[[], None]) -> None:
        pass


class StoppedPresence(Visible, ThreadStatus, declared_name=None):
    stopped = True
    vacant = True

    def registered(self, thread: PresenceThread, last_seen: float) -> ThreadStatus:
        """Stopped never keeps a process binding, so a stored stop still bound is a fence
        whose host has not completed the session."""
        return thread.process_identity.stopped_seen(self)

    def fenced(self) -> ThreadStatus:
        return StoppingThreadStatus()

    def require_stopped(self) -> None:
        pass

    def in_view(self, visibility: RosterVisibility) -> bool:
        return visibility.stopped


class StoppedThreadStatus(OrphansHolds, StoppedPresence):
    """Stopped for good: nothing is running to answer, and work must not wait on it. A session
    finishing its last turn can still write to what it holds, and one whose bridge dropped comes
    back on its own, so those members of StoppedPresence keep their holds."""


class StoppingThreadStatus(HostBoundPresence, StoppedPresence):
    """Fenced against new work while its host completes the session. The binding stays
    until that completion, so a restart cannot race it; a stop sent again completes a
    session the host no longer holds. It is stored as stopped: the kept binding is what
    tells it apart, so the registry's stored format does not change."""

    def declaration(self) -> ThreadStatus:
        return StoppedThreadStatus()


class CliInboxPresence(AlwaysListed):
    """The declared inbox resolves its bridge process at every read."""

    may_be_active: ClassVar[bool] = True

    def registered(self, thread: PresenceThread, last_seen: float) -> ThreadStatus:
        return (ConnectedThreadStatus() if thread.process_identity.alive()
                else NoBridgeThreadStatus())

    def restored(self) -> ThreadStatus:
        """Restoration preserves the inbox declaration, never a liveness answer."""
        return NoBridgeThreadStatus()

    def after_heartbeat(self, name: str) -> ThreadStatus:
        """A CLI command is not a bridge; a heartbeat keeps the inbox as it is."""
        self.require_mutable(name)
        return self


class DeclaredReaderPresence(CliInboxPresence):
    """The session bridge's reader state survives registration and storage."""

    def declaration(self) -> Self:
        return self

    def external_registered(self, thread: PresenceThread, now: float) -> ThreadStatus:
        return self.registered(thread, now)


class ConnectedThreadStatus(DeclaredReaderPresence, RunningThreadStatus):
    """A CLI inbox whose bridge process is watching it. A stopped or frozen process is still
    alive, so the connection is a claim; the inbox it stopped taking is the evidence."""

    def claimed_readers(self, name: str) -> tuple[str, ...]:
        return (name,)


class PausedBridgeThreadStatus(SelfRenaming, DeclaredReaderPresence, StoppedPresence):
    """A live session bridge waiting for a Monitor to confirm its probe."""

    def registered(self, thread: PresenceThread, last_seen: float) -> ThreadStatus:
        return self if thread.process_identity.alive() else NoBridgeThreadStatus()

    def paused_since(self, last_seen: float) -> tuple[float, ...]:
        return (last_seen,)

    @property
    def answers_later(self) -> bool:
        """Its live bridge keeps every delivery until a Monitor confirms the probe, so a
        wait on it is answered; only a dead bridge (NoBridgeThreadStatus) is not."""
        return True


class NoBridgeThreadStatus(SelfRenaming, CliInboxPresence, StoppedPresence):
    """A CLI inbox with no bridge attached; its messages wait unread.

    It has no launched process to stop or restart, so it stays in every view.
    """

    def paused_since(self, last_seen: float) -> tuple[float, ...]:
        return (last_seen,)

    def presentation(
        self, title: str, activity: ObservedActivity,
        idle: Callable[[ThreadPresentation], ThreadPresentation],
    ) -> ThreadPresentation:
        return replace(super().presentation(title, activity, idle), summary="CLI inbox · no bridge")


class ServiceThreadStatus(PublishingPresence, ThreadStatus):
    """A service can publish, but has no executable roster row or owner controls."""

    def in_view(self, visibility: RosterVisibility) -> bool:
        return False

    def external_registered(self, thread: PresenceThread, now: float) -> ThreadStatus:
        return self

    def after_heartbeat(self, name: str) -> ThreadStatus:
        return self

    def restored(self) -> ThreadStatus:
        return self


class ArchivedThreadStatus(OrphansHolds, ThreadStatus):
    archived = True
    vacant = True

    def in_view(self, visibility: RosterVisibility) -> bool:
        return visibility.archived

    def restored(self) -> ThreadStatus:
        return self


class DeletingThreadStatus(ThreadStatus):
    def in_view(self, visibility: RosterVisibility) -> bool:
        return False

    def require_deleting(self) -> None:
        pass

    def require_mutable(self, name: str) -> None:
        raise RelationViolationError(f"Thread {name!r} is being permanently deleted.")


class ThreadStatusName(TextRepresentation[ThreadStatus]):
    @classmethod
    def encode(cls, value: object) -> object:
        return FieldCodec.encode(type(value))

    @classmethod
    def from_text(cls, value: str) -> ThreadStatus:
        return ThreadStatus.member_named(value)()
