"""Threads: declaration and persistence owners."""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field, fields, replace
from pathlib import Path
from typing import TYPE_CHECKING

from .agent_backend import AgentBackend
from .child_process import NoProcess, ProcessIdentity, ProcessState
from .errors import RelationViolationError
from .field_codec import FieldCodec
from .cli_result import ThreadToolCatalogResult
from .launch_runtime import RestartEnvironment
from .native_tools import NativeRequestTools
from .store_files import _atomic_write_text
from .goals import (
    AbsentGoal,
    Goal,
    GoalCheckpoint,
    GoalRevision,
    PresentGoalCheckpoint,
    ThreadGoal,
)
from .pi_vocabulary import (
    NoThinkingOverride,
    ServiceTier,
    ThinkingChoice,
    ThinkingLevelField,
)
from .registration_inheritance import (
    InheritEmpty,
    InheritMissing,
    InheritPrevious,
    InheritThinkingLevel,
)
from .storage_rules import StoragePath, StorageRoots, StorageRule
from .thread_execution import NativeThreadExecution, ThreadExecution
from .thread_identity import (
    AgentThreadRole,
    ThreadProvenance,
    ThreadRole,
    TurnId,
    TurnIdentity,
)
from .turn_lease import NoTurnRecord, TurnFence, TurnLeaseFence, TurnRecord, TurnState

if TYPE_CHECKING:
    from .launch_runtime import ThreadLaunch


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
    process_identity: ProcessState = field(default_factory=NoProcess)
    session_file: str | None = field(
        default=None, kw_only=True, metadata={"registration_inheritance": InheritMissing}
    )
    model: str | None = field(default=None, metadata={"registration_inheritance": InheritMissing})
    thinking_level: ThinkingLevelField = field(
        default=NoThinkingOverride, metadata={"registration_inheritance": InheritThinkingLevel}
    )
    service_tier: ServiceTier = field(
        default=ServiceTier.PRIORITY, kw_only=True,
        metadata={"registration_inheritance": InheritPrevious},
    )
    goal: ThreadGoal = field(
        default_factory=AbsentGoal, metadata={"registration_inheritance": InheritMissing}
    )
    created_at: float = field(default_factory=_thread_creation_time, kw_only=True,
                              metadata={"wire_required": True})
    _generated_created_at: bool = field(init=False, default=False, repr=False, compare=False)
    storage_roots: StorageRoots = field(kw_only=True,
        metadata={"registration_inheritance": InheritPrevious})
    # Projects this conversation ran in before its current one: a saved session
    # resumes from them and a started turn finishes in one of them.
    earlier_projects: tuple[StoragePath, ...] = field(default=(), kw_only=True,
        metadata={"registration_inheritance": InheritPrevious, "wire_omit_default": True})
    auto_title_pending: bool = field(
        default=False, metadata={"registration_inheritance": InheritPrevious}
    )
    # The project changed during a turn, and no turn has started in it yet.
    # The next turn start clears this; an idle owner starts that turn itself.
    project_resume_due: bool = field(
        default=False,
        metadata={"registration_inheritance": InheritPrevious, "wire_omit_default": True},
    )
    title: str | None = field(default=None, kw_only=True,
                             metadata={"registration_inheritance": InheritMissing})
    role: type[ThreadRole] = AgentThreadRole
    execution: type[ThreadExecution] = field(default=NativeThreadExecution, kw_only=True,
                                            metadata={"wire_omit_default": True})
    backend: type[AgentBackend] = field(
        default_factory=AgentBackend.for_new_threads, kw_only=True,
        metadata={"wire_omit_default": True, "registration_inheritance": InheritPrevious},
    )
    turn_record: TurnRecord = field(default_factory=NoTurnRecord)
    channel_scope_generation: int = 0
    turn_generation: int = 0
    last_finished_turn_id: str | None = None

    def inherited_history(self, registry, visited):
        if self.name in visited:
            return
        visited.add(self.name)
        if self.parent is not None:
            parent = registry.require(self.parent)
            for location, _ in parent.runtime.history_locations(parent, registry, visited):
                yield location, True

    def contains_worktree(self, worktree: str, rule: StorageRule) -> bool:
        """A project this conversation ran in, or this unit's physical home and extra checkouts."""
        return self.ran_in(worktree) or Path(worktree).expanduser().resolve() in rule.checkouts_of(self)

    def ran_in(self, worktree: str) -> bool:
        """The current project or one this conversation moved away from."""
        selected = Path(worktree).expanduser().resolve()
        return selected == Path(self.worktree).expanduser().resolve() or selected in self.earlier_projects

    def moved_to_project(self, project: str) -> Thread:
        """A running turn finishes where it started and owes one turn in the new project."""
        earlier = Path(self.worktree).expanduser().resolve()
        return replace(
            self, worktree=project,
            earlier_projects=tuple(dict.fromkeys((*self.earlier_projects, earlier))),
            project_resume_due=self.project_resume_due or self.executing,
        )

    @property
    def owes_project_resume(self) -> bool:
        """Idle with the resume still due; a turn for the active goal is that turn."""
        return self.active_goal.owes_project_resume(self.project_resume_due and not self.executing)

    @property
    def turn_state(self) -> TurnState:
        return TurnState(self.turn_record.read(), self.last_finished_turn_id)

    def restart_candidates(self, status):
        return self.execution.restart_candidates(self, status)

    def require_restart_owner(self, status) -> None:
        if not self.restart_candidates(status):
            raise RelationViolationError("Restart requires another live owner")

    def native_environment(self, root, snapshot, worktree: str) -> dict[str, str]:
        """Project this captured owner through the original native binding."""
        from .runtime_requests import ProjectRuntimeRequest

        project = ProjectRuntimeRequest.for_native(snapshot, self)
        return {
            **replace(self.launch(), worktree=worktree, managed=True).encode(),
            "AGENT_COMMS_ROOT": str(root),
            **project.environment(root),
        }

    def native_launch_arguments(self, arguments: tuple[str, ...]) -> tuple[str, ...]:
        """Write this native launch's catalog in its config directory, not a CLI cache."""
        directory = RestartEnvironment.inherit(os.environ).native_config
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        path = directory / f"comms-catalog-{self.incarnation.require_recorded().lookup}.json"
        catalog = ThreadToolCatalogResult(tuple(NativeRequestTools.catalog()), self.service_tier)
        _atomic_write_text(path, json.dumps(catalog.encoded(), separators=(",", ":")))
        return (*arguments, "--comms-catalog", str(path))

    def launch(self) -> ThreadLaunch:
        """The launch environment this thread's owner process carries."""
        from .launch_runtime import ThreadLaunch

        return ThreadLaunch(self.name, self.worktree, frozenset(self.tags), self.parent, self.task)

    def __post_init__(self) -> None:
        generated = isinstance(self.created_at, _GeneratedCreationTime)
        object.__setattr__(self, "_generated_created_at", generated)
        if generated:
            object.__setattr__(self, "created_at", float(self.created_at))
        super(Thread, self).__post_init__()
        self.incarnation.require_recorded()  # Archives may lack a birth; live threads may not.
        self.process_identity.require_held_by(self.role)
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

    def require_original_model_configuration(self, original: Thread) -> None:
        changed = any((
            self.incarnation != original.incarnation,
            self.worktree != original.worktree,
            self.model != original.model,
            self.thinking_level != original.thinking_level,
            self.service_tier != original.service_tier,
        ))
        if changed:
            raise RelationViolationError("Thread configuration changed before publication")

    def with_model_configuration(
        self, *, model: str | None = None,
        thinking_level: type[ThinkingChoice] = NoThinkingOverride,
        service_tier: ServiceTier | None = None,
    ) -> Thread:
        return replace(
            self,
            model=self.model if model is None else model.strip(),
            thinking_level=thinking_level.applied(self.thinking_level),
            service_tier=self.service_tier if service_tier is None else service_tier,
        )

    def needs_model_observation(self) -> bool:
        """Only an unset declaration needs a saved or live model observation."""
        return self.model is None

    def initialized_native_configuration(
        self, *, model: str | None, thinking_level: type[ThinkingChoice],
    ) -> Thread:
        """The native producer may fill unset fields, never replace a selection."""
        return replace(
            self,
            model=model if self.needs_model_observation() else self.model,
            thinking_level=self.thinking_level.observed(thinking_level),
        )

    @property
    def process_alive(self) -> bool:
        return self.process_identity.alive()

    @classmethod
    def require_declaration(cls, value: Thread) -> None:
        if type(value) is not cls:
            raise ValueError("live owner requires an exact thread declaration")

    def same_local_process(self, process: ProcessIdentity) -> bool:
        return self.process_identity == process

    def require_local_process(self, process: ProcessIdentity) -> None:
        self.role.require_executable(RelationViolationError("A human thread owns no local process"))
        if not self.same_local_process(process):
            raise RelationViolationError("live owner is not the current process incarnation")

    def require_process(self) -> ProcessIdentity:
        self.role.require_executable(
            RelationViolationError(f"Thread {self.name!r} is human; it owns no process")
        )
        return self.process_identity.require(f"Thread {self.name!r} has no owner process")


    def require_idle(self) -> None:
        if self.executing:
            raise RelationViolationError("live owner already has an active turn")

    def require_current_turn(self, claim_scope: int) -> None:
        self.turn_record.read().require_current(claim_scope, self.turn_generation)

    def require_turn(self, turn_id: TurnId, claim_scope: int) -> None:
        lease = self.turn_lease
        if lease is None or not lease.can_attest(claim_scope) or TurnId(lease.turn_id) != turn_id:
            raise RelationViolationError("live owner does not hold the requested turn")

    def require_goal_checkpoint(self, checkpoint: GoalRevision) -> Goal:
        if self.goal_checkpoint != PresentGoalCheckpoint(checkpoint):
            raise ValueError("The goal changed before this action; the action was not applied.")
        return self.goal.require_set()

    @property
    def active_goal(self) -> ThreadGoal:
        """Project the original goal's declaration; retain no activity copy."""
        return self.goal.active

    @property
    def task_scope(self):
        return self.active_goal.task_scope(self.worktree)

    def context_goal_segments(self, goals):
        """Project this declaration's active goal without a second goal state."""
        return self.active_goal.context_segments(self, goals)

    def wake_context(self) -> dict[str, object]:
        """This captured thread owns its selected-wake work and goal projection."""
        return {
            "name": self.name, "title": self.title, "tags": sorted(self.tags),
            "original_assignment": self.task, "current_goal": self.goal.wake_view(),
        }

    def retained_task_facts(self):
        return self.goal.task_facts()

    def continuation_goal(self, original: Thread) -> ThreadGoal:
        """The current active goal still belongs to this captured project/goal."""
        if self.worktree != original.worktree:
            return AbsentGoal()
        return self.active_goal.continuing(original.active_goal)

    def goal_for(self, goal_id: str) -> ThreadGoal:
        """Select the current record of this goal, including later state/revisions."""
        return self.goal.matching(goal_id)

    def require_goal_replacement(self, goals) -> None:
        self.goal.require_replacement(goals, self.incarnation)

    def require_goal(self, goal_id: str | None = None) -> Goal:
        if goal_id is None:
            return self.goal.require_set()
        return self.goal_for(goal_id).require_current()

    def require_active_goal(self, goal_id: str) -> Goal:
        goal = self.require_goal(goal_id)
        goal.state.require_active()
        return goal

    @property
    def goal_checkpoint(self) -> GoalCheckpoint:
        return self.goal.goal_checkpoint

    def same_launch(self, other: Thread) -> bool:
        """Whether both records describe one process serving one saved session with one model setting.

        Something read from the running agent under one record still describes the
        other only when this holds. Titles, tags, goals and turns do not change it.
        """
        def launch(thread: Thread):
            return (thread.incarnation, thread.process_identity, thread.role, thread.runtime,
                    thread.worktree, thread.execution, thread.model, thread.thinking_level)
        return launch(self) == launch(other)

    def without_turn(self) -> Thread:
        """Drop the reference; the registry transaction owns revoking the record."""
        return replace(self, turn_record=NoTurnRecord())

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
                    if self.turn_record == previous.turn_record else None
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
        lease = self.turn_lease
        return lease.identity if lease is not None else None

    @property
    def turn_lease(self) -> TurnLeaseFence | None:
        """Read this record's exact turn once; no phase is cached on the thread."""
        return self.turn_record.lease(self.incarnation, self.process_identity, self.turn_generation)

    def require_turn_lease(self) -> TurnLeaseFence:
        lease = self.turn_lease
        if lease is None:
            raise RelationViolationError("An authorized original turn is required")
        return lease

    def has_authored_turn(self, identity: TurnIdentity, turn: TurnId) -> bool:
        lease = self.turn_lease
        return lease is not None and (lease.identity, lease.turn_id) == (identity, turn.value)

    def has_observed_task_turn(self, identity: TurnIdentity, turn: TurnId) -> bool:
        """Bind a certified authored row to this preparation's original turn cut.

        Beginning a lease clears last_finished_turn_id. The preceding allocation
        is still derived from the original monotonic turn owner, not copied into
        a timing register. Its row supplied the original authorized ID/generation.
        Neither allocation nor finishing supplies subtask completion.
        """
        if identity.incarnation != self.incarnation:
            return False
        lease = self.turn_lease
        if lease is not None:
            return self.has_authored_turn(identity, turn) or identity.generation == lease.identity.generation - 1
        return identity.generation == self.turn_generation and self.last_finished_turn_id == turn.value

    def observed_turn(self, claim_scope: int) -> TurnFence | None:
        """Passive current/last-completed witness; never a begin-turn grant."""
        turn_id = self.turn_record.read().observed_id(self.last_finished_turn_id)
        if turn_id is None:
            return None
        return TurnFence(TurnIdentity(self.incarnation, self.turn_generation), turn_id, claim_scope)

    def has_goal_revision(self, goal_id: str, minimum_revision: int) -> bool:
        return self.goal.accepts_observation(goal_id, minimum_revision)

    @property
    def is_fork(self) -> bool:
        return self.parent is not None

    def turn_started_by(self, updated_at_ms: int) -> bool:
        """Read the original start only when this decision needs it."""
        return self.turn_record.read().started_by(updated_at_ms)

    @property
    def executing(self) -> bool:
        return self.turn_record.executing

    def to_wire(self) -> dict[str, object]:
        """Schema-derived projection at a JSON boundary, not a hand-maintained mirror."""
        return FieldCodec.encode(self)


def current_thread(registry) -> Thread:
    """Declare the current thread from its owner launch environment."""
    from .launch_runtime import ThreadLaunch

    launch = ThreadLaunch.current()
    return registry.new_thread(
        name=launch.name,
        tags=launch.tags,
        worktree=launch.worktree,
        parent=launch.parent,
        task=launch.task,
        process_identity=ProcessIdentity.capture(os.getpid()),
    )


# Thread's backend field is the lookup boundary. Declare both implementations
# after the record graph, then reject incomplete declarations before any lookup.
from . import pi_native_backend, pi_durable_backend  # noqa: E402, F401

AgentBackend.seal()
