"""Threads: declaration and persistence owners."""

from __future__ import annotations

import json
import os
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import TYPE_CHECKING, ClassVar, Annotated


from .agent_backend import AgentBackend, NewThreadBackend
from .child_process import NoProcess, ProcessIdentity, ProcessState
from .cli_result import ThreadToolCatalogResult
from .context_segments.wake_values import WakeWorkContext
from .errors import RelationViolationError
from .field_codec import (
    Argument, Description, FieldCodec, NullMember, RegistrationPolicy,
    SingleFieldValue, SubclassUnion, WireOmitDefault, WireRequired,
)
from .goals import (
    GoalDeclaration,
    NoGoal,
)
from .launch_root import LaunchRoot
from .lifecycle_capability import Executing
from .locked_store import DurableFile, PatternFile
from .native_arguments import NativeArguments
from .native_tools import NativeRequestTools
from .pi_vocabulary import (
    NoThinkingOverride,
    ServiceTier,
    ThinkingChoice,
    ThinkingLevelField,
)
from .registration_inheritance import (
    InheritApplied,
    InheritEmpty,
    InheritPrevious,
)
from .storage_rules import StoragePath, StorageRoots, StorageRule
from .store_files import _atomic_write_text
from .task_sources import TaskScope
from .thread_execution import NativeThreadExecution, ThreadExecution
from .thread_model import DeclaredModel, ThreadModel, UndeclaredModel
from .thread_text import NoParent, NoThreadText, ThreadParent, ThreadText
from .session_file import NoSessionFile, SessionFile
from .thread_provenance import ThreadProvenance
from .thread_identity import (
    AgentThreadRole,
    ThreadRole,
    TurnFence,
    TurnId,
    TurnIdentity,
)
from .turn_lease import FinishedTurn, NoTurnRecord, TurnLease, TurnLeaseFence, TurnRecord, TurnState

if TYPE_CHECKING:
    from .goal_management import Goals
    from .launch_runtime import ThreadLaunch


class _GeneratedCreationTime(float):
    """Transient marker for default timestamps; never persisted as claim authority."""


def _thread_creation_time() -> float:
    return _GeneratedCreationTime(time.time())


class NextTurnSetting(ABC):
    """One next-turn setting a configuration request either sets or keeps."""

    @abstractmethod
    def configured(self, thread: Thread) -> Thread: ...


class KeptSetting(NextTurnSetting):
    """The request leaves the thread's current value in place."""

    def configured(self, thread: Thread) -> Thread:
        return thread


class ModelChoice(NextTurnSetting, SubclassUnion, affix="Model"):
    pass


@dataclass(frozen=True, slots=True)
class ChosenModel(SingleFieldValue, ModelChoice):
    model: DeclaredModel

    def configured(self, thread: Thread) -> Thread:
        return replace(thread, model=self.model)


@dataclass(frozen=True, slots=True)
class KeptModel(NullMember, KeptSetting, ModelChoice):
    pass


class ServiceTierChoice(NextTurnSetting, SubclassUnion, affix="ServiceTier"):
    pass


@dataclass(frozen=True, slots=True)
class ChosenServiceTier(SingleFieldValue, ServiceTierChoice):
    tier: ServiceTier

    def configured(self, thread: Thread) -> Thread:
        return replace(thread, service_tier=self.tier)


@dataclass(frozen=True, slots=True)
class KeptServiceTier(NullMember, KeptSetting, ServiceTierChoice):
    pass


@dataclass(frozen=True, kw_only=True)
class NextTurnConfiguration:
    """The model, thinking level and service tier one request sets or keeps for the next turn."""

    model: Annotated[
        ModelChoice, Description("Provider/model identifier"), Argument(flags=("--model",))
    ] = KeptModel()
    thinking_level: Annotated[
        ThinkingLevelField, Description("Optional Pi thinking level"),
        Argument(flags=("--thinking",)),
    ] = NoThinkingOverride
    service_tier: Annotated[
        ServiceTierChoice, Description("Optional provider request service tier"),
        Argument(flags=("--service-tier",)),
    ] = KeptServiceTier()

    def configured(self, thread: Thread) -> Thread:
        thought = replace(thread, thinking_level=self.thinking_level.applied(thread.thinking_level))
        return self.service_tier.configured(self.model.configured(thought))


@dataclass(frozen=True, slots=True)
class Thread(DurableFile, PatternFile, ThreadProvenance):
    filename: ClassVar[str] = "native-sessions/{lookup}/{name}"
    catalog_filename: ClassVar[str] = "comms-catalog.json"
    """Declares one agent thread's identity and provenance."""

    name: str = field(kw_only=False)
    tags: Annotated[frozenset[str], RegistrationPolicy(InheritEmpty)] = field(
        kw_only=False
    )
    worktree: str = field(kw_only=False)
    parent: ThreadParent = field(default_factory=NoParent)
    task: ThreadText = field(default_factory=NoThreadText)
    process_identity: ProcessState = field(default_factory=NoProcess)
    session_file: Annotated[SessionFile, RegistrationPolicy(InheritApplied)] = (
        field(default_factory=NoSessionFile, kw_only=True)
    )
    model: Annotated[ThreadModel, RegistrationPolicy(InheritApplied)] = field(
        default_factory=UndeclaredModel
    )
    thinking_level: Annotated[
        ThinkingLevelField, RegistrationPolicy(InheritApplied)
    ] = NoThinkingOverride
    service_tier: Annotated[ServiceTier, RegistrationPolicy(InheritPrevious)] = (
        field(default=ServiceTier.PRIORITY, kw_only=True)
    )
    goal: Annotated[GoalDeclaration, RegistrationPolicy(GoalDeclaration)] = (
        field(default_factory=NoGoal)
    )
    created_at: Annotated[float, WireRequired()] = field(
        default_factory=_thread_creation_time, kw_only=True
    )
    _generated_created_at: bool = field(init=False, default=False, repr=False, compare=False)
    storage_roots: Annotated[
        StorageRoots, RegistrationPolicy(InheritPrevious)
    ] = field(kw_only=True)
    # Projects this conversation ran in before its current one: a saved session
    # resumes from them and a started turn finishes in one of them.
    earlier_projects: Annotated[
        tuple[StoragePath, ...],
        RegistrationPolicy(InheritPrevious),
        WireOmitDefault(),
    ] = field(default=(), kw_only=True)
    auto_title_pending: Annotated[bool, RegistrationPolicy(InheritPrevious)] = (
        False
    )
    # The project changed during a turn, and no turn has started in it yet.
    # The next turn start clears this; an idle owner starts that turn itself.
    project_resume_due: Annotated[
        bool, RegistrationPolicy(InheritPrevious), WireOmitDefault()
    ] = False
    title: Annotated[ThreadText, RegistrationPolicy(InheritApplied)] = field(
        default_factory=NoThreadText, kw_only=True
    )
    role: Annotated[type[ThreadRole], RegistrationPolicy(InheritPrevious)] = (
        AgentThreadRole
    )
    execution: Annotated[type[ThreadExecution], WireOmitDefault()] = field(
        default=NativeThreadExecution, kw_only=True
    )
    backend: Annotated[
        type[AgentBackend],
        WireOmitDefault(),
        RegistrationPolicy(InheritPrevious),
    ] = field(default_factory=NewThreadBackend.selected, kw_only=True)
    turn_record: TurnRecord = field(default_factory=NoTurnRecord)
    channel_scope_generation: int = 0
    turn_generation: int = 0

    def inherited_history(self, registry, visited):
        if self.name in visited:
            return
        visited.add(self.name)
        for name in self.parent.names():
            parent = registry.require(name)
            for location, _ in parent.runtime.history_locations(parent, registry, visited):
                yield location, True

    def contains_worktree(self, worktree: str, rule: StorageRule) -> bool:
        """A project this conversation ran in, or this unit's physical home and extra checkouts."""
        return self.ran_in(worktree) or Path(worktree).expanduser().resolve() in rule.checkouts_of(self)

    @property
    def project_path(self) -> Path:
        """The physical current project declared by this thread."""
        return Path(self.worktree).expanduser().resolve()

    def ran_in(self, worktree: str) -> bool:
        """The current project or one this conversation moved away from."""
        selected = Path(worktree).expanduser().resolve()
        return selected == self.project_path or selected in self.earlier_projects

    def moved_to_project(self, project: str) -> Thread:
        """A running turn finishes where it started and owes one turn in the new project."""
        earlier = self.project_path
        current = Path(project).expanduser().resolve()
        return replace(
            self, worktree=project,
            earlier_projects=tuple(dict.fromkeys(
                path for path in (*self.earlier_projects, earlier) if path != current
            )),
            project_resume_due=self.project_resume_due or self.executing,
        )

    @property
    def owes_project_resume(self) -> bool:
        """Idle with the resume still due; a turn for the active goal is that turn."""
        return self.project_resume_due and not self.executing and not self.goal.accepts_following_handoff()

    @property
    def turn_state(self) -> TurnState:
        return self.turn_record.state()

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
            **project.environment(root),
        }

    def native_launch_arguments(
        self, arguments: NativeArguments, package: Path, root: Path,
    ) -> tuple[str, ...]:
        """The existing native session directory owns this root's launch catalog."""
        if not arguments.loads(NativeRequestTools.native_extension(package)):
            return arguments.argv
        launch = LaunchRoot.from_path(root)
        directory = self.native_session_directory(launch.path)
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        path = self.path_in(launch.path, lookup=self.incarnation.require_recorded().lookup,
                            name=self.catalog_filename)
        catalog = ThreadToolCatalogResult(
            tuple(NativeRequestTools.catalog()), self.service_tier, launch.arguments(),
        )
        _atomic_write_text(path, json.dumps(catalog.to_wire(), separators=(",", ":")))
        return (*arguments.argv, "--comms-catalog", str(path))

    def launch(self) -> ThreadLaunch:
        """The launch environment this thread's owner process carries."""
        from .launch_runtime import ThreadLaunch

        return ThreadLaunch.from_thread(self)

    def native_session_directory(self, root: Path) -> Path:
        return self.path_in(root, lookup=self.incarnation.require_recorded().lookup, name="")

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
        self.turn_record.require_generation(self.turn_generation)
        if self.parent.named(self.name):
            raise RelationViolationError(f"Thread {self.name!r} cannot be its own parent.")

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

    def initialized_native_configuration(
        self, *, model: ThreadModel, thinking_level: type[ThinkingChoice],
    ) -> Thread:
        """The native producer may fill unset fields, never replace a selection."""
        return replace(
            self,
            model=self.model.applied(model),
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
        if not self.turn_lease.attests(claim_scope, turn_id):
            raise RelationViolationError("live owner does not hold the requested turn")

    @property
    def task_scope(self) -> TaskScope:
        return TaskScope.for_goal(self.worktree, self.goal)

    def wake_context(self, goals: Goals) -> WakeWorkContext:
        return WakeWorkContext.from_thread(self, goals)

    def continuation_goal(self, original: Thread) -> GoalDeclaration:
        """The current active goal still belongs to this captured project/goal."""
        if self.worktree != original.worktree:
            return NoGoal()
        return original.goal.active().continued_by(self.goal.active())

    def same_launch(self, other: Thread) -> bool:
        """Whether both records describe one process serving one saved session with one model setting.

        Something read from the running agent under one record still describes the
        other only when this holds. Titles, tags, goals and turns do not change it.
        """
        def launch(thread: Thread):
            return (thread.incarnation, thread.process_identity, thread.role, thread.runtime,
                    thread.worktree, thread.execution, thread.model, thread.thinking_level)
        return launch(self) == launch(other)

    def same_turn(self, other: Thread) -> bool:
        """Compare captured references and epochs, including captures made while idle.

        Reading the mutable turn files again could make two revoked leases look
        equal. The saved references and registry generations retain their fence.
        """
        return (
            self.turn_record == other.turn_record
            and self.turn_generation == other.turn_generation
        )

    def without_turn(self) -> Thread:
        """Drop the reference; the registry transaction owns revoking the record."""
        return replace(self, turn_record=self.turn_record.released())

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
                result, turn_generation=previous.turn_generation, turn_record=previous.turn_record,
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
                plan.name: directive.value.choose(
                    getattr(self, plan.name), getattr(previous, plan.name))
                for plan in FieldCodec.field_plans(type(self))
                for directive in RegistrationPolicy.single(plan.annotation)
            }
        resolved = replace(self, name=canonical_name, **inherited)
        return self if resolved == self else resolved

    @property
    def turn_lease(self) -> TurnLease:
        """Read this record's exact turn once; no phase is cached on the thread."""
        return self.turn_record.lease(self.incarnation, self.process_identity, self.turn_generation)

    def require_turn_lease(self) -> TurnLeaseFence:
        return self.turn_lease.require(RelationViolationError("An authorized original turn is required"))

    def has_authored_turn(self, identity: TurnIdentity, turn: TurnId) -> bool:
        return self.turn_lease.authored(identity, turn)

    def has_observed_task_turn(self, identity: TurnIdentity, turn: TurnId) -> bool:
        """Check whether a task record can refer to this thread's current or preceding turn.

        Reject a different thread registration. During a turn, accept its exact
        ID and generation or the immediately preceding generation. When idle,
        require a matching generation and a recorded finished turn.
        Neither starting nor finishing a turn proves a subtask was completed.
        """
        if identity.incarnation != self.incarnation:
            return False
        return self.turn_lease.observes_task_turn(
            identity, turn,
            idle=identity.generation == self.turn_generation
            and self.turn_record == FinishedTurn(turn.value))

    @property
    def turn_identity(self) -> TurnIdentity:
        """The turn generation this thread's registry entry currently names."""
        return TurnIdentity(self.incarnation, self.turn_generation)

    def observed_turn(self, claim_scope: int) -> TurnFence | None:
        """Passive current/last-completed witness; never a begin-turn grant."""
        turn_id = self.turn_state.observed_id()
        if turn_id is None:
            return None
        return TurnFence(self.turn_identity, turn_id, claim_scope)

    def latest_idle_turn(self, fence: TurnFence, claim_scope: int) -> bool:
        """Only the exact latest completed turn can release a dependency."""
        return not self.executing and self.observed_turn(claim_scope) == fence

    @property
    def is_fork(self) -> bool:
        return self.parent.forked

    def turn_started_by(self, updated_at_ms: int) -> bool:
        """Read the original start only when this decision needs it."""
        return self.turn_record.read().started_by(updated_at_ms)

    @property
    def executing(self) -> bool:
        return bool(self.turn_record.items_with(Executing, self))

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
from . import pi_durable_backend, pi_native_backend  # noqa: E402, F401

AgentBackend.seal()
