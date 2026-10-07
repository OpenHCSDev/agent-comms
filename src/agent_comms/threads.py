"""Threads: declaration and persistence owners."""

from __future__ import annotations

import os
import time
from pathlib import Path
from dataclasses import dataclass, field, fields, replace
from typing import TYPE_CHECKING

from .child_process import ProcessIdentity
from .errors import RelationViolationError, UnregisteredThreadError
from .field_codec import FieldCodec
from .thread_provenance import ThreadProvenance
from .thread_execution import ThreadExecution, NativeThreadExecution
from .pi_vocabulary import ThinkingLevel
from .goals import (
    AbsentGoalCheckpoint, Goal, GoalCheckpoint, GoalRevision, PresentGoalCheckpoint,
)
from .registration_inheritance import InheritEmpty, InheritMissing, InheritPrevious
from .thread_identity import (
    ThreadIncarnation,
    ThreadPublicationIdentity,
    ThreadRole,
    TurnId,
    TurnIdentity,
)
from .turn_lease import ActiveTurn, TurnFence, TurnLeaseFence, TurnState

if TYPE_CHECKING:
    from .owner_compaction_gate import OwnerCompactionAttestation
    from .owner_compaction_prepare import NativeWitness



class _GeneratedCreationTime(float):
    """Transient marker for default timestamps; never persisted as claim authority."""


def _thread_creation_time() -> float:
    return _GeneratedCreationTime(time.time())


@dataclass(frozen=True, slots=True)
class Thread(ThreadProvenance):
    """Declares one agent thread's identity and provenance."""

    name: str = field(kw_only=False)
    tags: frozenset[str] = field(kw_only=False, metadata={"registration_inheritance": InheritEmpty})
    worktree: str = field(kw_only=False)
    parent: str | None = None
    task: str | None = None
    process_identity: ProcessIdentity | None = None
    session_file: str | None = field(
        default=None, kw_only=True, metadata={"registration_inheritance": InheritMissing}
    )
    model: str | None = field(default=None, metadata={"registration_inheritance": InheritMissing})
    thinking_level: type[ThinkingLevel] | None = field(
        default=None, metadata={"registration_inheritance": InheritMissing}
    )
    goal: Goal | None = field(default=None, metadata={"registration_inheritance": InheritMissing})
    created_at: float = field(default_factory=_thread_creation_time, kw_only=True,
                              metadata={"wire_required": True})
    _generated_created_at: bool = field(init=False, default=False, repr=False, compare=False)
    previous_worktrees: tuple[str, ...] = field(
        default=(), metadata={"registration_inheritance": InheritEmpty}
    )
    auto_title_pending: bool = field(
        default=False, metadata={"registration_inheritance": InheritPrevious}
    )
    title: str | None = field(default=None, kw_only=True,
                             metadata={"registration_inheritance": InheritMissing})
    role: ThreadRole = ThreadRole.AGENT
    execution: type[ThreadExecution] = field(default=NativeThreadExecution, kw_only=True,
                                            metadata={"wire_omit_default": True})
    active_turn: ActiveTurn | None = None
    channel_scope_generation: int = 0
    turn_generation: int = 0
    last_finished_turn_id: str | None = None

    def contains_worktree(self, worktree: str) -> bool:
        """This conversation's current and retained project directories."""
        selected = str(Path(worktree).expanduser().resolve())
        return any(selected == str(Path(path).expanduser().resolve())
                   for path in (self.worktree, *self.previous_worktrees))

    @property
    def turn_state(self) -> TurnState:
        return TurnState(self.active_turn, self.last_finished_turn_id)

    def restart_candidates(self, status):
        return self.execution.restart_candidates(self, status)

    def require_restart_owner(self, status) -> None:
        self.execution.require_native()
        self.role.require_executable()
        status.require_active()
        if not self.process_alive or self.pid == os.getpid():
            raise RelationViolationError("Restart requires another live owner")

    def native_environment(self, root, snapshot, worktree: str) -> dict[str, str]:
        """Project this captured owner through the original native binding."""
        from .runtime_requests import ProjectRuntimeRequest

        project = ProjectRuntimeRequest.for_native(snapshot, self)
        return {
            "AGENT_COMMS_THREAD": self.name,
            "PI_AGENT_ID": self.name,
            "AGENT_COMMS_ROOT": str(root),
            "PI_PARENT_ID": self.parent or "",
            "AGENT_COMMS_MANAGED": "1",
            "PI_WORKTREE": worktree,
            **project.environment(root),
        }

    def __post_init__(self) -> None:
        generated = isinstance(self.created_at, _GeneratedCreationTime)
        object.__setattr__(self, "_generated_created_at", generated)
        if generated:
            object.__setattr__(self, "created_at", float(self.created_at))
        super(Thread, self).__post_init__()
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
        if self.parent == self.name:
            raise RelationViolationError(f"Thread {self.name!r} cannot be its own parent.")
        if self.model is not None and not self.model.strip():
            raise ValueError("Thread model cannot be empty.")
        if self.thinking_level is not None:
            object.__setattr__(self, "thinking_level", ThinkingLevel.field_value(self.thinking_level))


    def initialized_native_configuration(
        self, *, model: str | None, thinking_level: str | None
    ) -> Thread:
        """The native producer may fill unset fields, never replace a selection."""
        return replace(
            self,
            model=self.model if self.model is not None else model,
            thinking_level=(
                self.thinking_level if self.thinking_level is not None else thinking_level
            ),
        )

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

    def require_process(self) -> ProcessIdentity:
        self.role.require_executable()
        if self.process_identity is None:
            raise RelationViolationError(f"Thread {self.name!r} has no owner process")
        return self.process_identity

    def compaction_attestation(self, owner_generation: int, witness: NativeWitness) -> OwnerCompactionAttestation:
        """Project this captured owner; registry and native CAS still recheck it."""
        from pathlib import Path

        from .owner_compaction_gate import OwnerCompactionAttestation

        if self.active_turn is None or self.session_file is None:
            raise ValueError("Claimed owner with canonical session required")
        session = str(Path(self.session_file).resolve(strict=True))
        witness.require_session(session)
        return OwnerCompactionAttestation(
            self.name, owner_generation, self.active_turn.id,
            self.goal.id if self.goal is not None else None,
            self.goal.revision if self.goal is not None else None,
            session, witness.leaf_id, witness.revision, None,
        )

    def require_saved_session(self) -> str:
        if self.session_file is None:
            raise ValueError("Canonical saved session required")
        return self.session_file

    def imported_sources(self):
        """Absent native selection has no imported historical instruction sources."""
        from pathlib import Path
        from .native_transcript import NativeTranscript

        if self.session_file is None:
            return ()
        return NativeTranscript(Path(self.session_file)).imported_sources()

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

    def require_goal_checkpoint(self, checkpoint: GoalRevision) -> Goal:
        if self.goal_checkpoint != PresentGoalCheckpoint(checkpoint):
            raise ValueError("The goal changed before this action; the action was not applied.")
        assert self.goal is not None
        return self.goal

    @property
    def active_goal(self) -> Goal | None:
        """Project the original goal's declaration; retain no activity copy."""
        goal = self.goal
        return goal if goal is not None and goal.state.active else None

    @property
    def task_scope(self):
        from .task_sources import GoalTaskScope, TurnTaskScope

        goal = self.active_goal
        return (GoalTaskScope(project=self.worktree, goal=goal.checkpoint)
                if goal is not None else TurnTaskScope(project=self.worktree))

    def context_goal_segments(self):
        """Project this declaration's active goal without a second goal state."""
        from .turn_context import GoalSegment

        goal = self.active_goal
        return (GoalSegment.capture(self, goal),) if goal is not None else ()

    def wake_context(self) -> dict[str, object]:
        """This captured thread owns its selected-wake work and goal projection."""
        goal = self.goal
        return {
            "name": self.name, "title": self.title, "tags": sorted(self.tags),
            "original_assignment": self.task,
            "current_goal": (None if goal is None else {
                "text": goal.text, "status": goal.state.declared_name,
                "progress": goal.progress,
            }),
        }

    def retained_task_facts(self):
        from .retained_task_facts import GoalTaskFact

        return (GoalTaskFact(self.goal),) if self.goal is not None else ()

    def continuation_goal(self, original: Thread) -> Goal | None:
        """The current active goal still belongs to this captured project/goal."""
        if self.worktree != original.worktree:
            return None
        goal, captured = self.active_goal, original.active_goal
        if goal is None or captured is None:
            return None
        return goal if goal.id == captured.id else None

    def goal_for(self, goal_id: str) -> Goal | None:
        """Select the current record of this goal, including later state/revisions."""
        goal = self.goal
        return goal if goal is not None and goal.id == goal_id else None

    def require_goal(self, goal_id: str | None = None) -> Goal:
        if goal_id is None:
            if self.goal is None:
                raise ValueError("No goal is set for this thread.")
            return self.goal
        goal = self.goal_for(goal_id)
        if goal is None:
            raise RelationViolationError("This goal was replaced or cleared; refresh its state.")
        return goal

    def require_active_goal(self, goal_id: str) -> Goal:
        goal = self.require_goal(goal_id)
        goal.state.require_active()
        return goal

    @property
    def goal_checkpoint(self) -> GoalCheckpoint:
        return (PresentGoalCheckpoint(self.goal.checkpoint)
                if self.goal is not None else AbsentGoalCheckpoint())

    @property
    def has_process(self) -> bool:
        return self.process_identity is not None

    @property
    def publication_identity(self) -> ThreadPublicationIdentity:
        return ThreadPublicationIdentity(
            self.incarnation, self.process_identity, self.role, self.session_file, self.worktree,
            self.execution,
        )

    def without_turn_admission(self) -> Thread:
        if self.active_turn is None:
            return self
        return replace(self, active_turn=replace(self.active_turn, admission_generation=None))

    def preserve_registration_history(self, previous: Thread) -> Thread:
        """Metadata writers cannot forge channel scope or reset a finished turn."""
        scope = previous.channel_scope_generation
        if self.tags != previous.tags:
            if scope >= (1 << 63) - 1:
                raise RelationViolationError("Channel scope generation exhausted")
            scope += 1
        result = replace(self, created_at=previous.created_at, channel_scope_generation=scope)
        if self.turn_generation != previous.turn_generation:
            result = replace(
                result, turn_generation=previous.turn_generation,
                last_finished_turn_id=(
                    previous.last_finished_turn_id
                    if self.active_turn == previous.active_turn else None
                ),
            )
        return result

    def for_claim(self, name: str) -> Thread:
        """Assign an allocated name without turning a generated clock into caller identity."""
        created = _GeneratedCreationTime(self.created_at) if self._generated_created_at else self.created_at
        return replace(self, name=name, created_at=created)

    def for_registration(self, canonical_name: str, previous: Thread | None) -> Thread:
        """Resolve metadata once from its field declarations, retaining exact identity.

        An unchanged declaration is returned intact: replacing it would discard
        the transient generated-creation marker used by the locked registry to
        resolve clock collisions. Executor and turn authority are not metadata.
        """
        inherited = {}
        if previous is not None:
            inherited = {
                declared.name: policy.choose(
                    getattr(self, declared.name), getattr(previous, declared.name)
                )
                for declared in fields(self)
                if (policy := declared.metadata.get("registration_inheritance")) is not None
            }
        resolved = replace(self, name=canonical_name, **inherited)
        return self if resolved == self else resolved

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

    def require_turn_lease(self) -> TurnLeaseFence:
        lease = self.turn_lease
        if lease is None:
            raise RelationViolationError("An admitted original turn is required")
        return lease

    def has_authored_turn(self, identity: TurnIdentity, turn: TurnId) -> bool:
        lease = self.turn_lease
        return lease is not None and (lease.identity, lease.turn_id) == (identity, turn.value)

    def has_observed_task_turn(self, identity: TurnIdentity, turn: TurnId) -> bool:
        """Bind a certified authored row to this preparation's original turn cut.

        Beginning a lease clears last_finished_turn_id. The preceding allocation
        is still derived from the original monotonic turn owner, not copied into
        a timing register. Its row supplied the original admitted ID/generation.
        Neither allocation nor finishing supplies subtask completion.
        """
        if identity.incarnation != self.incarnation:
            return False
        lease = self.turn_lease
        if lease is not None:
            return self.has_authored_turn(identity, turn) or identity.generation == lease.identity.generation - 1
        return identity.generation == self.turn_generation and self.last_finished_turn_id == turn.value

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

    def turn_started_by(self, updated_at_ms: int) -> bool:
        """A declared turn already belongs to this executor (enforced at decode)."""
        return self.active_turn is not None and self.active_turn.started_at * 1000 <= updated_at_ms + 1

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
