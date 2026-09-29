"""Threads: declaration and persistence owners."""

from __future__ import annotations

import os
import time
from dataclasses import dataclass, field

from .channel_targets import Tag
from .child_process import ProcessIdentity
from .errors import RelationViolationError, UnregisteredThreadError
from .field_codec import FieldCodec
from .goals import Goal, GoalRevision
from .thread_identity import OwnerIdentity, ThreadIncarnation, ThreadRole, TurnId, TurnIdentity
from .turn_lease import ActiveTurn, TurnFence, TurnLeaseFence


class _GeneratedCreationTime(float):
    """Transient marker for default timestamps; never persisted as claim authority."""


def _thread_creation_time() -> float:
    return _GeneratedCreationTime(time.time())


@dataclass(frozen=True, slots=True)
class Thread:
    """Declares one agent thread's identity and provenance."""

    name: str
    tags: frozenset[str]
    worktree: str
    parent: str | None = None
    task: str | None = None
    process_identity: ProcessIdentity | None = None
    session_file: str | None = None
    model: str | None = None
    thinking_level: str | None = None
    goal: Goal | None = None
    created_at: float = field(default_factory=_thread_creation_time)
    _generated_created_at: bool = field(init=False, default=False, repr=False, compare=False)
    previous_worktrees: tuple[str, ...] = ()
    auto_title_pending: bool = False
    title: str | None = None
    role: ThreadRole = ThreadRole.AGENT
    active_turn: ActiveTurn | None = None
    last_goal_report_turn: str | None = None
    channel_scope_generation: int = 0
    turn_generation: int = 0
    last_finished_turn_id: str | None = None

    def __post_init__(self) -> None:
        generated = isinstance(self.created_at, _GeneratedCreationTime)
        object.__setattr__(self, "_generated_created_at", generated)
        if generated:
            object.__setattr__(self, "created_at", float(self.created_at))
        object.__setattr__(self, "role", ThreadRole(self.role))
        if self.active_turn is not None:
            if self.active_turn.owner_pid != self.pid:
                raise RelationViolationError("A turn must belong to the registered executor.")
            if self.active_turn.turn_generation is not None and (
                type(self.active_turn.turn_generation) is not int
                or self.active_turn.turn_generation <= 0
                or self.active_turn.turn_generation != self.turn_generation
            ):
                raise RelationViolationError("Active turn generation differs from its owner.")
        if self.last_goal_report_turn is not None and not isinstance(
            self.last_goal_report_turn, str
        ):
            raise ValueError("Last goal report turn must be a string or null.")
        if (
            type(self.channel_scope_generation) is not int
            or not 0 <= self.channel_scope_generation < 1 << 63
        ):
            raise ValueError("Channel scope generation must be a nonnegative 63-bit integer.")
        if type(self.turn_generation) is not int or not 0 <= self.turn_generation < 1 << 63:
            raise ValueError("Turn generation must be an exact nonnegative 63-bit integer.")
        if self.last_finished_turn_id is not None and (
            type(self.last_finished_turn_id) is not str
            or not self.last_finished_turn_id
            or self.turn_generation == 0
        ):
            raise ValueError("Finished turn requires a prior turn generation and ID.")
        for tag in self.tags:
            Tag(tag)
        allowed = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_")
        if not self.name or not set(self.name) <= allowed:
            raise ValueError(
                f"Thread name {self.name!r} must be alphanumeric with hyphens/underscores."
            )
        if self.parent == self.name:
            raise RelationViolationError(f"Thread {self.name!r} cannot be its own parent.")
        if not self.worktree:
            raise ValueError("Thread worktree cannot be empty.")
        if self.model is not None and not self.model.strip():
            raise ValueError("Thread model cannot be empty.")
        if self.thinking_level is not None and self.thinking_level not in {
            "off",
            "minimal",
            "low",
            "medium",
            "high",
            "xhigh",
            "max",
        }:
            raise ValueError("Unknown thinking level.")

    @property
    def pid(self) -> int:
        """Numeric OS projection of the sole stored process authority."""
        return self.process_identity.pid if self.process_identity is not None else 0

    @property
    def process_alive(self) -> bool:
        return self.process_identity is not None and self.process_identity.alive()

    @classmethod
    def require_declaration(cls, value: Thread) -> None:
        if type(value) is not cls:
            raise ValueError("live owner requires an exact thread declaration")

    def require_local_process(self, process: ProcessIdentity) -> None:
        self.role.require_executable()
        if self.process_identity != process:
            raise RelationViolationError("live owner is not the current process incarnation")

    def require_idle(self) -> None:
        if self.active_turn is not None:
            raise RelationViolationError("live owner already has an active turn")

    def require_current_turn(self, admission: int) -> None:
        turn = self.active_turn
        if turn is not None and not turn.current(admission, self.turn_generation):
            raise RelationViolationError("live owner turn admission is no longer current")

    def require_turn(self, turn_id: TurnId, admission: int) -> None:
        self.require_current_turn(admission)
        turn = self.active_turn
        if turn is None or TurnId(turn.id) != turn_id:
            raise RelationViolationError("live owner does not hold the requested turn")

    @property
    def goal_checkpoint(self) -> GoalRevision | None:
        return self.goal.checkpoint if self.goal is not None else None

    @property
    def incarnation(self) -> ThreadIncarnation:
        return ThreadIncarnation(self.name, self.created_at)

    def owner_identity(self, generation: int) -> OwnerIdentity:
        return OwnerIdentity(self.incarnation, generation)

    @property
    def turn_identity(self) -> TurnIdentity | None:
        turn = self.active_turn
        if turn is None or turn.admission_generation is None or turn.turn_generation is None:
            return None
        return TurnIdentity(self.incarnation, turn.turn_generation)

    @property
    def turn_lease(self) -> TurnLeaseFence | None:
        """Capture this snapshot's exact turn; historical unattested turns have no lease."""
        identity = self.turn_identity
        if identity is None:
            return None
        assert self.active_turn is not None
        assert self.active_turn.admission_generation is not None
        return TurnLeaseFence(
            identity=identity,
            turn_id=self.active_turn.id,
            admission_generation=self.active_turn.admission_generation,
        )

    def observed_turn(self, admission: int) -> TurnFence | None:
        """Passive current/last-completed witness; never a begin-turn grant."""
        turn_id = self.active_turn.id if self.active_turn is not None else self.last_finished_turn_id
        if turn_id is None:
            return None
        return TurnFence(TurnIdentity(self.incarnation, self.turn_generation), turn_id, admission)

    def has_goal_revision(self, goal_id: str, minimum_revision: int) -> bool:
        return self.goal is not None and self.goal.accepts_observation(goal_id, minimum_revision)

    @property
    def is_fork(self) -> bool:
        return self.parent is not None

    @property
    def executing(self) -> bool:
        return self.active_turn is not None

    def to_wire(self) -> dict[str, object]:
        """Schema-derived projection at a JSON boundary, not a hand-maintained mirror."""
        return FieldCodec.encode(self)


def current_thread() -> Thread:
    """Declare the current thread from process environment.

    Fail-closed: raises if neither PI_AGENT_ID nor AGENT_COMMS_THREAD is set.
    """
    name = os.environ.get("PI_AGENT_ID") or os.environ.get("AGENT_COMMS_THREAD")
    if not name:
        raise UnregisteredThreadError(
            "PI_AGENT_ID is not set. This process is not an orchestrated thread."
        )
    tag_env = os.environ.get("PI_AGENT_TAGS") or os.environ.get("AGENT_COMMS_TAGS", "")
    tags = frozenset(t.strip() for t in tag_env.split(",") if t.strip())
    return Thread(
        name=name,
        tags=tags,
        worktree=os.environ.get("PI_WORKTREE", os.getcwd()),
        parent=os.environ.get("PI_PARENT_ID"),
        task=os.environ.get("PI_TASK"),
        process_identity=ProcessIdentity.capture(os.getpid()),
    )
