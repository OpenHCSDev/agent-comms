"""Thread presence, lifecycle decisions and their display/control projections."""

from __future__ import annotations

import time
from abc import abstractmethod
from collections.abc import Callable
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, ClassVar, Self

from .child_process import ProcessIdentity, ProcessState
from .declared_family import DeclaredFamily
from .errors import RelationViolationError, ThreadNotStoppedError
from .field_codec import FieldCodec, TextRepresentation
from .lifecycle_capability import LifecycleCapability
from .thread_presentation import (
    InactiveThreadPresentationStatus,
    LiveThreadOwnerBinding,
    ThreadOwnerBinding,
    ThreadPresentation,
    UnavailableThreadOwnerBinding,
)
from .turn_lease import (
    FinishedTurnFence,
    NoFinishedTurn,
)

if TYPE_CHECKING:
    from .activity import ObservedActivity
    from .owner_lifecycle import OwnerLifecycle
    from .registration import Registration
    from .registry_document import RegistryEntry, RegistrySnapshot, RosterVisibility
    from .thread_identity import GenerationCounter, OwnerIdentity
    from .threads import Thread
    from .tool_caller import ToolCaller
    from .turn_lease import TurnCompletion, TurnLeaseFence


class AlwaysListed:
    """Presence listed independently of stopped/archived roster filters."""

    def in_view(self, visibility: RosterVisibility) -> bool:
        return True


class SelfRenaming:
    """The current participant may rename its own inbox."""

    def require_self_rename(self) -> None:
        pass


class Visible:
    """A presence that can be addressed, started and pinned in channels."""

    def require_visible(self, message: str = "Initial direct target must be visible.") -> None:
        pass


@dataclass(frozen=True)
class ThreadStatus(LifecycleCapability, DeclaredFamily, affix="ThreadStatus"):
    may_be_active: ClassVar[bool] = False
    running: ClassVar[bool] = False
    stopped: ClassVar[bool] = False
    # Whether a message sent now is read now; each status answers, none by default.
    receiving: ClassVar[bool] = False

    @abstractmethod
    def in_view(self, visibility: RosterVisibility) -> bool:
        """Whether this lifecycle belongs in the requested executable roster."""

    def registered(self, thread: Thread, last_seen: float) -> ThreadStatus:
        return self

    def declaration(self) -> ThreadStatus:
        return self

    def external_registered(self, thread: Thread, now: float) -> ThreadStatus:
        return NoBridgeThreadStatus().registered(thread, now)

    def paused_since(self, last_seen: float) -> tuple[float, ...]:
        return ()

    @property
    def answers_later(self) -> bool:
        """Whether a wait on this thread is answered once it reads; a running thread does."""
        return self.running

    def require_visible(self, message: str = "Initial direct target must be visible.") -> None:
        raise RelationViolationError(message)

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

    def permits_reconfiguration(self, caller: ToolCaller, registry: Registration, name: str) -> bool:
        return True

    def native_owner_binding(self, snapshot: RegistrySnapshot, thread: Thread) -> ThreadOwnerBinding:
        return UnavailableThreadOwnerBinding()

    def restart_candidates(self, thread: Thread) -> tuple[Thread, ...]:
        return ()

    def settle_local_stop(self, lifecycle: OwnerLifecycle, thread: Thread) -> bool:
        return thread.process_identity.allows_inactive_stop()

    def deletable_threads(self, thread: Thread) -> tuple[Thread, ...]:
        return () if thread.process_alive else (thread,)

    def owns_entry(self, entry: RegistryEntry, owner: OwnerIdentity, process: ProcessIdentity) -> bool:
        return False

    def completed_turn(self, lease: TurnLeaseFence) -> TurnCompletion:
        return NoFinishedTurn()

    def restored(self) -> ThreadStatus:
        """Restoring saved provenance never restarts its original process."""
        return StoppedThreadStatus()

    def after_heartbeat(self, name: str) -> ThreadStatus:
        self.require_mutable(name)
        return ProcessThreadStatus()

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

    def presentation(
        self, title: str, activity: ObservedActivity,
        idle: Callable[[ThreadPresentation], ThreadPresentation],
    ) -> ThreadPresentation:
        return ThreadPresentation(title, "○", self.declared_name.title(),
                                  status=InactiveThreadPresentationStatus())


class ActiveThreadPresence(AlwaysListed, Visible, ThreadStatus, declared_name=None):
    """Shared semantics of running and idle owners; not a second state family."""

    receiving = True
    may_be_active = True

    def registered(self, thread: Thread, last_seen: float) -> ThreadStatus:
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

    def permits_reconfiguration(self, caller: ToolCaller, registry: Registration, name: str) -> bool:
        return caller.matches_thread(registry, name)

    def native_owner_binding(self, snapshot: RegistrySnapshot, thread: Thread) -> ThreadOwnerBinding:
        if not thread.process_alive:
            return super().native_owner_binding(snapshot, thread)
        return LiveThreadOwnerBinding(snapshot.owner_identity(thread.name), thread.require_process())

    def restart_candidates(self, thread: Thread) -> tuple[Thread, ...]:
        return thread.role.executor(thread) if thread.process_alive else ()

    def settle_local_stop(self, lifecycle: OwnerLifecycle, thread: Thread) -> bool:
        if not thread.process_identity.runs_here():
            return False
        lifecycle._settle_local_owner(thread)
        return True

    def deletable_threads(self, thread: Thread) -> tuple[Thread, ...]:
        return ()

    def owns_entry(self, entry: RegistryEntry, owner: OwnerIdentity, process: ProcessIdentity) -> bool:
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


class StoppedThreadStatus(Visible, ThreadStatus):
    stopped = True

    def require_stopped(self) -> None:
        pass

    def in_view(self, visibility: RosterVisibility) -> bool:
        return visibility.stopped


class CliInboxPresence(AlwaysListed):
    """The declared inbox resolves its bridge process at every read."""

    may_be_active: ClassVar[bool] = True

    def registered(self, thread: Thread, last_seen: float) -> ThreadStatus:
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

    def external_registered(self, thread: Thread, now: float) -> ThreadStatus:
        return self.registered(thread, now)


class ConnectedThreadStatus(DeclaredReaderPresence, RunningThreadStatus):
    """A CLI inbox whose bridge process is watching it."""


class PausedBridgeThreadStatus(SelfRenaming, DeclaredReaderPresence, StoppedThreadStatus):
    """A live session bridge waiting for a Monitor to confirm its probe."""

    def registered(self, thread: Thread, last_seen: float) -> ThreadStatus:
        return self if thread.process_identity.alive() else NoBridgeThreadStatus()

    def paused_since(self, last_seen: float) -> tuple[float, ...]:
        return (last_seen,)

    @property
    def answers_later(self) -> bool:
        """Its live bridge keeps every delivery until a Monitor confirms the probe, so a
        wait on it is answered; only a dead bridge (NoBridgeThreadStatus) is not."""
        return True


class NoBridgeThreadStatus(SelfRenaming, CliInboxPresence, StoppedThreadStatus):
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


class ThreadStatusName(TextRepresentation[ThreadStatus]):
    @classmethod
    def encode(cls, value: object) -> object:
        return FieldCodec.encode(type(value))

    @classmethod
    def from_text(cls, value: str) -> ThreadStatus:
        return ThreadStatus.member_named(value)()
