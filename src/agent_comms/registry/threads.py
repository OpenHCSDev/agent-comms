"""Threads: declaration and persistence owners."""

from __future__ import annotations

import os
import time
from abc import ABC, abstractmethod
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, fields, replace
from functools import cached_property
from pathlib import Path
from typing import Annotated, ClassVar

from agent_comms.agent_backend import AgentBackend, NewThreadBackend, RuntimeLocation
from agent_comms.context_segments.wake_values import WakeWorkContext, WakeWorkSource
from agent_comms.errors import RelationViolationError
from agent_comms.execution.child_process import NoProcess, ProcessIdentity, ProcessState
from agent_comms.execution.launch_runtime import ThreadLaunch
from agent_comms.execution.native_arguments import SelectedModel
from agent_comms.execution.pi_vocabulary import (
    NoThinkingOverride,
    ServiceTier,
    ThinkingChoice,
    ThinkingLevelField,
)
from agent_comms.execution.session_file import NoSessionFile, SessionFile
from agent_comms.execution.turn_lease import (
    FinishedTurn,
    NoTurnRecord,
    StoredTurnRecord,
    TurnDeclaration,
    TurnLease,
    TurnLeaseFence,
    TurnRecord,
    TurnState,
)
from agent_comms.messaging.channel_sources import ChannelMemberSource
from agent_comms.messaging.channel_targets import DeclaredTag, Tag
from agent_comms.nominal.declared_family import DeclaredFamily
from agent_comms.nominal.field_codec import (
    Argument,
    Catalog,
    Description,
    FieldCodec,
    NullMember,
    RegistrationPolicy,
    SingleFieldValue,
    SubclassUnion,
    TurnId,
    WireOmitDefault,
    WireRequired,
)
from agent_comms.nominal.lifecycle import LifecycleState
from agent_comms.nominal.lifecycle_capability import Executing
from agent_comms.nominal.registration_inheritance import (
    InheritApplied,
    InheritEmpty,
    InheritPrevious,
)
from agent_comms.planning.goal_failure_owner import GoalFailureOwner
from agent_comms.planning.goal_mutation import GoalFork
from agent_comms.planning.goals import GoalConsentRequired, GoalDeclaration, NoGoal
from agent_comms.planning.task_scope import TaskScope
from agent_comms.projects.checkouts import Checkout, RepositoryReads
from agent_comms.registry.activity import ObservedActivity
from agent_comms.registry.heartbeat_delivery import (
    HeartbeatDelivery,
    OffHeartbeatDelivery,
)
from agent_comms.registry.presence_reads import PresenceThread
from agent_comms.registry.tagged_thread_source import TaggedThreadSource
from agent_comms.registry.task_owner import TaskOwner
from agent_comms.registry.thread_creation import (
    CreationSeconds,
    CreationTime,
    GeneratedCreationTime,
)
from agent_comms.registry.thread_identity import (
    AgentThreadRole,
    ObservedTurn,
    OfferedTools,
    ThreadIncarnation,
    ThreadIncarnationRecord,
    ThreadRole,
    TurnFence,
    TurnIdentity,
    UnobservedTools,
)
from agent_comms.registry.thread_incarnation_read import ReadsThreadIncarnation
from agent_comms.registry.thread_model import (
    CatalogSelection,
    DeclaredModel,
    ModelSelection,
    NamedModel,
    ThreadModel,
    UndeclaredModel,
    UnselectedModel,
)
from agent_comms.registry.thread_presentation import (
    ThreadOwnerBinding,
    ThreadPresentation,
    UnavailableThreadOwnerBinding,
)
from agent_comms.registry.thread_reads import ThreadCheckoutSource, ThreadOwnerSource
from agent_comms.registry.thread_settings import (
    ModelSetting,
    ServiceTierSetting,
    ThinkingSetting,
    ThreadSettings,
)
from agent_comms.registry.thread_status import ServiceThreadStatus, ThreadStatus
from agent_comms.registry.thread_storage import (
    AssignedStorageRoots,
    ThreadStorage,
    UnassignedThreadStorage,
)
from agent_comms.registry.thread_text import (
    NoParent,
    NoThreadText,
    ThreadParent,
    ThreadText,
)
from agent_comms.storage.locked_store import DurableFile, PatternFile


class ThreadCommandAvailability:
    @classmethod
    def available_for(cls, status: ThreadStatus, *, process: ProcessState) -> bool:
        return True

    @classmethod
    def available_for_thread(cls, thread: Thread, status: ThreadStatus) -> bool:
        return cls.available_for(status, process=thread.process_identity)


class NativeOwnerCommand(ThreadCommandAvailability):
    @classmethod
    def available_for_thread(cls, thread: Thread, status: ThreadStatus) -> bool:
        return thread.native_command_available(cls, status)


class OwnerLifecycleControl(ThreadCommandAvailability):
    @classmethod
    def available_for(cls, status: ThreadStatus, *, process: ProcessState) -> bool:
        return status.allows_owner_control()


class StartOwnerControl(OwnerLifecycleControl):
    @classmethod
    def available_for(cls, status: ThreadStatus, *, process: ProcessState) -> bool:
        return status.allows_owner_start(process=process)


class ArchiveOwnerControl(OwnerLifecycleControl):
    @classmethod
    def available_for(cls, status: ThreadStatus, *, process: ProcessState) -> bool:
        return status.is_stopped()


class ConversationPreparation[T](ABC):
    @abstractmethod
    def native(self) -> T: ...

    @abstractmethod
    def external(self) -> T: ...


class NoChannelInbox:
    """A producer or parent-bound child has no independent channel inbox."""

    __slots__ = ()

    def channel_recipients(self) -> tuple[Thread, ...]:
        return ()



@dataclass(frozen=True)
class ThreadWorkState(DeclaredFamily, LifecycleState, affix="ThreadWorkState"):
    """Order availability belongs to the thread, independently of process presence."""

    @classmethod
    def successors(cls) -> tuple[type[LifecycleState], ...]:
        return ThreadWorkState.members_with(ThreadWorkState)

    @abstractmethod
    def candidates(self, thread: Thread) -> tuple[Thread, ...]: ...

    @abstractmethod
    def require_assignment(self) -> None: ...


class ReadyThreadWorkState(ThreadWorkState):
    def candidates(self, thread: Thread) -> tuple[Thread, ...]:
        return (thread,)

    def require_assignment(self) -> None:
        pass


class ParkedThreadWorkState(ThreadWorkState):
    def candidates(self, thread: Thread) -> tuple[Thread, ...]:
        return ()

    def require_assignment(self) -> None:
        raise RelationViolationError(
            "The teammate parked this thread; resume it before assigning work."
        )


class NextTurnSetting(ABC):
    """One next-turn setting a configuration request either sets or keeps."""

    @abstractmethod
    def configured(self, thread: Thread) -> Thread: ...


class KeptSetting(NextTurnSetting):
    """The request leaves the thread's current value in place."""

    def configured(self, thread: Thread) -> Thread:
        return thread


class ModelChoice(NextTurnSetting, SubclassUnion, ModelSelection, affix="Model"):
    pass


@dataclass(frozen=True, slots=True)
class ChosenModel(NamedModel, ModelChoice):
    def __post_init__(self) -> None:
        """A choice the owner cannot launch is refused here, not at the next wake."""
        NamedModel.__post_init__(self)
        SelectedModel.decode(self.text)

    def configured(self, thread: Thread) -> Thread:
        return replace(thread, model=DeclaredModel(self.text))


@dataclass(frozen=True, slots=True)
class KeptModel(UnselectedModel, KeptSetting, ModelChoice):
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
        ModelChoice, Catalog(ModelSetting), Argument(flags=("--model",))
    ] = KeptModel()
    thinking_level: Annotated[
        ThinkingLevelField, Catalog(ThinkingSetting),
        Argument(flags=("--thinking",)),
    ] = NoThinkingOverride
    service_tier: Annotated[
        ServiceTierChoice, Catalog(ServiceTierSetting),
        Argument(flags=("--service-tier",)),
    ] = KeptServiceTier()

    def configured(self, thread: Thread) -> Thread:
        thought = replace(thread, thinking_level=self.thinking_level.applied(thread.thinking_level))
        return self.service_tier.configured(self.model.configured(thought))


@dataclass(frozen=True, kw_only=True)
class ThreadProvenance(TaggedThreadSource):
    """Recorded thread identity, without permission to own a process or start a turn."""

    name: str = field(kw_only=False)
    tags: Annotated[frozenset[str], RegistrationPolicy(InheritEmpty)] = field(kw_only=False)
    worktree: Checkout = field(kw_only=False)
    created_at: Annotated[CreationTime, CreationSeconds, WireRequired()]
    session_file: Annotated[SessionFile, RegistrationPolicy(InheritApplied)] = field(
        default_factory=NoSessionFile
    )
    title: Annotated[ThreadText, RegistrationPolicy(InheritApplied)] = field(
        default_factory=NoThreadText
    )
    backend: Annotated[
        type[AgentBackend], WireOmitDefault(), RegistrationPolicy(InheritPrevious)
    ] = field(default_factory=NewThreadBackend.selected)

    @cached_property
    def runtime(self) -> RuntimeLocation:
        """The codec decoded both stored fields; their record owns this binding."""
        return self.session_file.location(self.backend)

    @cached_property
    def writable_runtime(self) -> RuntimeLocation:
        """The runtime a launch writes; a copied bus refuses a harness transcript."""
        return self.session_file.writable_location(self.backend)

    @property
    def display_title(self) -> str:
        return self.title.otherwise(self.name)

    @staticmethod
    def require_name(name: str) -> None:
        allowed = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_")
        if not name or not set(name) <= allowed:
            raise ValueError(f"Thread name {name!r} must be alphanumeric with hyphens/underscores.")

    def __post_init__(self) -> None:
        object.__setattr__(self, "created_at", FieldCodec.decode(
            Annotated[CreationTime, CreationSeconds], self.created_at))
        self.require_name(self.incarnation.name)
        for tag in self.tags:
            Tag.validate(tag)

    @staticmethod
    def capture(thread: ThreadProvenance) -> ThreadProvenance:
        """Freeze only determining provenance from the original typed declaration."""
        return ThreadProvenance(
            **{
                item.name: getattr(thread, item.name)
                for item in fields(ThreadProvenance)
                if item.init
            }
        )

    @cached_property
    def incarnation(self) -> ThreadIncarnation:
        """The declaration's birth member, created once and shared by its readers."""
        return ThreadIncarnationRecord(self.name, self.created_at.to_wire()).incarnation()

    @property
    def acting_identity(self) -> ThreadIncarnation:
        """Provenance alone grants no work authority or parent representation."""
        return self.incarnation


class ThreadCatalogFile(DurableFile, PatternFile):
    """The catalog's path declaration, usable without importing the registry reader."""

    __slots__ = ()
    filename: ClassVar[str] = "native-sessions/{lookup}/comms-catalog.json"


@dataclass(frozen=True, slots=True)
class Thread(
    ThreadSettings,
    ThreadProvenance,
    CatalogSelection,
    DeclaredFamily,
    TaskOwner,
    GoalFailureOwner,
    ChannelMemberSource,
    PresenceThread,
    affix='Thread',
):
    """Declares one agent thread's identity and provenance; each member is how it executes.

    The registration owns transport intent; presence does not imply an RPC.
    """

    family_discriminator: ClassVar[str] = "execution"
    # What a refusal says when an operation needs this member and the thread is another.
    required_refusal: ClassVar[str] = "This thread does not have the execution this needs."

    @property
    def acting_identity(self) -> ThreadIncarnation:
        """Only independent executors maintain shared work; children bind at tool entry."""
        return next((peer.incarnation.require_recorded().as_colleague()
                     for peer in self.role.independent_threads(self)), self.incarnation)

    def answering_thread(self, reader: Thread) -> Thread:
        """A live declaration answers what it owes in its own transcript, whoever reads the debt."""
        return self

    parent: ThreadParent = field(default_factory=NoParent)
    work_state: Annotated[
        ThreadWorkState, RegistrationPolicy(InheritPrevious), WireOmitDefault()
    ] = field(default_factory=ReadyThreadWorkState, kw_only=True)
    heartbeat_delivery: Annotated[
        type[HeartbeatDelivery], RegistrationPolicy(InheritPrevious), WireOmitDefault()
    ] = field(default=OffHeartbeatDelivery, kw_only=True)
    task: ThreadText = field(default_factory=NoThreadText)
    process_identity: Annotated[ProcessState, RegistrationPolicy(InheritApplied)] = field(
        default_factory=NoProcess
    )
    goal: Annotated[GoalDeclaration, RegistrationPolicy(GoalDeclaration)] = (
        field(default_factory=NoGoal)
    )
    created_at: Annotated[CreationTime, CreationSeconds, WireRequired()] = field(
        default_factory=GeneratedCreationTime.now, kw_only=True
    )
    storage_roots: Annotated[
        ThreadStorage, AssignedStorageRoots, RegistrationPolicy(InheritPrevious), WireRequired()
    ] = field(default_factory=UnassignedThreadStorage, kw_only=True)
    # Projects this conversation ran in before its current one: a saved session
    # resumes from them and a started turn finishes in one of them.
    earlier_projects: Annotated[
        tuple[Checkout, ...],
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
    role: Annotated[type[ThreadRole], RegistrationPolicy(ThreadRole)] = (
        AgentThreadRole
    )
    turn_record: TurnRecord = field(default_factory=NoTurnRecord)
    channel_scope_generation: int = 0
    turn_generation: int = 0
    offered: Annotated[OfferedTools, WireOmitDefault()] = field(
        default_factory=UnobservedTools, kw_only=True
    )
    offered_before_restart: Annotated[OfferedTools, WireOmitDefault()] = field(
        default_factory=UnobservedTools, kw_only=True
    )

    def observing_tools(self, offered: OfferedTools) -> Thread:
        """Fill this incarnation; observations cannot manufacture a restart."""
        if self.offered == offered or not self.offered.filled_by(offered):
            return self
        return replace(self, offered=offered)

    def lost_tools(self) -> tuple[str, ...]:
        return self.offered.changes_from(self.offered_before_restart)

    def restarted_tools(self) -> Thread:
        """A new owner incarnation starts unobserved over the last real record."""
        return replace(self, offered=UnobservedTools(),
                       offered_before_restart=self.offered.restarted_over(self.offered_before_restart))

    def inherited_history(self, registry, visited):
        if self.name in visited:
            return
        visited.add(self.name)
        for name in self.parent.names():
            parent = registry.require(name)
            for location, view in parent.runtime.history_locations(parent, registry, visited):
                yield location, location.view_from_fork(view, self.incarnation.wire_birth)

    def contains_worktree(self, worktree: Checkout, rule: ThreadCheckoutSource[Thread]) -> bool:
        """A project this conversation ran in, or this unit's physical home and extra checkouts."""
        return self.ran_in(worktree) or worktree.resolved().path in rule.checkouts_of(
            self, RepositoryReads())

    @cached_property
    def project_path(self) -> Path:
        """The physical current project, resolved once for this immutable snapshot."""
        return self.worktree.resolved().path

    @property
    def home_checkouts(self) -> tuple[Path, ...]:
        """The current project when it is a Git checkout; a unit that has not yet run
        `agent-comms checkout add` from a plain directory such as its home holds none."""
        return Checkout.git_roots(self.project_path)

    def ran_in(self, worktree: Checkout) -> bool:
        """The current project or one this conversation moved away from."""
        selected = worktree.resolved()
        return selected == self.worktree.resolved() or selected in {
            project.resolved() for project in self.earlier_projects}

    def relocated_checkout(self, source: Checkout, destination: Checkout) -> Thread:
        """Replace only references to a physically relocated checkout; keep turn/history facts."""
        original = source.resolved()
        return replace(
            self,
            worktree=destination if self.worktree.resolved() == original else self.worktree,
            earlier_projects=tuple(destination if project.resolved() == original else project
                                   for project in self.earlier_projects),
        )

    def moved_to_project(self, project: Checkout) -> Thread:
        """A running turn finishes where it started and owes one turn in the new project."""
        earlier = self.worktree.resolved()
        current = project.resolved()
        return replace(
            self, worktree=current,
            earlier_projects=tuple(dict.fromkeys(
                path for path in (*self.earlier_projects, earlier) if path != current
            )),
            project_resume_due=self.project_resume_due or self.executing,
        )

    def continues_running_project(self, original: Thread) -> bool:
        """A selected next project does not move this exact turn's execution directory."""
        return self.worktree == original.worktree or (
            self.project_resume_due
            and self.turn_lease == original.require_turn_lease()
            and self.ran_in(original.worktree)
        )

    @property
    def accepts_following_input(self) -> bool:
        """A changed project needs a fresh native turn, not the old child's inbox."""
        return not self.project_resume_due and self.turn_state.active.phase.accepts_followup()

    @property
    def owes_project_resume(self) -> bool:
        """Idle with the resume still due; a turn for the active goal is that turn."""
        return self.project_resume_due and not self.executing and not self.goal.accepts_following_handoff()

    @property
    def turn_state(self) -> TurnState:
        return self.turn_record.state()

    def restart_candidates(self, status: ThreadStatus) -> tuple[Thread, ...]:
        return ()

    def teammate(self) -> str:
        """The teammate whose turn this thread works in: its board, its authority and its
        requests. An independent session is its own."""
        return self.name

    def prepare_conversation[T](self, preparation: ConversationPreparation[T]) -> T:
        return preparation.external()

    def native_command_available(self, command: type[ThreadCommandAvailability],
                                 status: ThreadStatus) -> bool:
        return False

    def registered_status(self, requested: ThreadStatus) -> ThreadStatus:
        """The lifecycle a registration installs; Core launched this owner."""
        return requested

    def withdrawn(self) -> Thread:
        """Unregistered: no session runs anywhere, so the row keeps no process binding."""
        return replace(self, process_identity=NoProcess())

    def order_candidates(self) -> tuple[Thread, ...]:
        """Ready executors with a free goal slot; idle time is not consent to replace work."""
        return tuple(candidate for executor in self.role.executor(self)
                     for ready in self.work_state.candidates(executor)
                     for candidate in ready.goal.work_candidates(ready))

    def channel_recipients(self) -> tuple[Thread, ...]:
        """The thread receives channel messages independently of its parent."""
        return (self,)

    def independent_threads(self) -> tuple[Thread, ...]:
        """An independent session owns its actions, regardless of its fork ancestry;
        only an executable role contributes one."""
        return self.role.executor(self)

    def forks(self) -> tuple[Thread, ...]:
        """The thread when it is a fork working inside its teammate's turn."""
        return ()

    def owner_binding(self, snapshot: ThreadOwnerSource[Thread]) -> ThreadOwnerBinding:
        return UnavailableThreadOwnerBinding()

    def presentation(self, status: ThreadStatus, ordinary: ThreadPresentation,
                     binding: ThreadOwnerBinding) -> ThreadPresentation:
        return ordinary

    def holds_activity(self, status: ThreadStatus) -> bool:
        """An executing native turn keeps its last activity current."""
        return self.executing

    def project_activity(self, activity: ObservedActivity,
                         turns: Mapping[StoredTurnRecord, TurnDeclaration]) -> ObservedActivity:
        """External participants report their own activity events."""
        return activity

    @classmethod
    def stored[Kind: Thread](cls: type[Kind], thread: ThreadProvenance) -> Kind:
        """The one narrowing of a stored declaration to the member an operation needs: a
        registry holds threads as stored data, so their member is tested here, once."""
        if isinstance(thread, cls):
            return thread
        raise RelationViolationError(cls.required_refusal)

    def redeclared_child(self, execution: type[SubagentThread]) -> Thread:
        """This thread named again as a child; it keeps the execution it was declared with."""
        return execution.stored(self)

    def same_execution(self, other: Thread) -> bool:
        """Two declarations of one member; the class is the execution's identity."""
        return type(self) is type(other)

    def as_member(self, member: type[Thread], **changes: object) -> Thread:
        """The same declaration under another execution; replace() keeps the class."""
        return member(**{**{item.name: getattr(self, item.name) for item in fields(self)
                            if item.init}, **changes})

    def require_goal_assignment(self, actor: ThreadIncarnation) -> None:
        if not actor.represents(self.incarnation):
            raise GoalConsentRequired(self, actor)

    def require_restart_owner(self, status) -> None:
        if not self.restart_candidates(status):
            raise RelationViolationError("Restart requires another live owner")

    def launch(self) -> ThreadLaunch:
        """The launch environment this thread's owner process carries."""
        return ThreadLaunch.from_thread(self)

    def catalog_path(self, root: Path) -> Path:
        return ThreadCatalogFile.path_in(root, lookup=self.incarnation.require_recorded().lookup)

    def native_session_directory(self, root: Path) -> Path:
        return self.catalog_path(root).parent

    def __post_init__(self) -> None:
        super(Thread, self).__post_init__()
        self.incarnation.require_recorded()  # Archives may lack a birth; live threads may not.
        self.process_identity.require_held_by(self.role)
        if not 0 <= self.channel_scope_generation < 1 << 63:
            raise ValueError("Channel scope generation must be a nonnegative 63-bit integer.")
        if not 0 <= self.turn_generation < 1 << 63:
            raise ValueError("Turn generation must be an exact nonnegative 63-bit integer.")
        self.turn_record.require_generation(self.turn_generation)
        if self.parent.named(self.name):
            raise RelationViolationError(f"Thread {self.name!r} cannot be its own parent.")

    def require_original_project(self, original: Thread) -> None:
        if (self.incarnation, self.worktree) != (original.incarnation, original.worktree):
            raise RelationViolationError("Thread project changed before publication")

    def require_original_model_configuration(self, original: Thread) -> None:
        self.require_original_project(original)
        changed = any((
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

    def stop_names(self, release_external: Callable[[str], None]) -> str | None:
        """The names this thread leaves to the ordinary stop: an agent's owner is fenced and
        its runtime retires it."""
        return self.name

    def same_local_process(self, process: ProcessIdentity) -> bool:
        return self.process_identity == process

    def require_local_process(self, process: ProcessIdentity) -> None:
        self.role.require_executable(RelationViolationError("A human thread owns no local process"))
        if not self.same_local_process(process):
            raise RelationViolationError("live owner is not the current process incarnation")

    def require_restartable(self) -> None:
        """Idle, or wedged: its turn wrote nothing to its transcript for too long."""
        self.turn_record.read().require_restartable(self.runtime.last_written())

    def require_process(self) -> ProcessIdentity:
        self.role.require_executable(
            RelationViolationError(f"Thread {self.name!r} is human; it owns no process")
        )
        return self.process_identity.require(f"Thread {self.name!r} has no owner process")

    def require_idle(self) -> None:
        if self.executing:
            raise RelationViolationError("live owner already has an active turn")

    def require_publication_turn(self, claim_generation: Callable[[], int], turn_id: TurnId) -> None:
        self.turn_record.read().require_current(claim_generation(), self.turn_generation)
        self.turn_record.read().require_turn(turn_id)

    def require_turn(self, turn_id: TurnId, claim_scope: int) -> None:
        if not self.turn_lease.attests(claim_scope, turn_id):
            raise RelationViolationError("live owner does not hold the requested turn")

    @property
    def task_scope(self) -> TaskScope:
        return TaskScope.for_goal(self.worktree, self.goal)

    def wake_context(self, goals: WakeWorkSource[Thread]) -> WakeWorkContext:
        return goals.wake_context(self)

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
                    thread.worktree, type(thread), thread.model, thread.thinking_level)
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

    def without_process(self) -> Thread:
        """This record in another root: its turn and process stay with the root it came from."""
        return replace(self.without_turn(), process_identity=NoProcess())

    def preserve_registration_history(self, previous: Thread) -> Thread:
        """Metadata writers cannot forge channel scope or reset a finished turn."""
        scope = previous.channel_scope_generation
        if self.tags != previous.tags:
            if scope >= (1 << 63) - 1:
                raise RelationViolationError("Channel scope generation exhausted")
            scope += 1
        result = replace(self, created_at=previous.created_at, channel_scope_generation=scope,
                         offered=previous.offered,
                         offered_before_restart=previous.offered_before_restart)
        if self.turn_generation != previous.turn_generation:
            result = replace(
                result, turn_generation=previous.turn_generation, turn_record=previous.turn_record,
            )
        return result

    def for_claim(self, name: str) -> Thread:
        """Assign an allocated name, retaining the creation value's allocation policy."""
        return replace(self, name=name)

    def for_registration(self, canonical_name: str, previous: Thread | None) -> Thread:
        """Resolve metadata once from its field declarations, retaining exact identity.

        Executor and turn authority are not metadata. The creation value retains
        its own allocation policy across replacements.
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
            and self.turn_record == FinishedTurn(turn))

    @property
    def turn_identity(self) -> TurnIdentity:
        """The turn generation this thread's registry entry currently names."""
        return TurnIdentity(self.incarnation, self.turn_generation)

    def observed_turn(self, claim_scope: int) -> ObservedTurn:
        """Passive current/last-completed witness; never a begin-turn grant."""
        return self.turn_state.active.observed_turn(
            self.turn_state.finished, self.turn_identity, claim_scope,
        )

    def latest_idle_turn(self, fence: TurnFence, claim_scope: int) -> bool:
        """Only the exact latest completed turn can release a dependency."""
        return not self.executing and self.observed_turn(claim_scope) == fence

    @property
    def is_fork(self) -> bool:
        return self.parent.is_forked()

    def turn_started_by(self, updated_at_ms: int) -> bool:
        """Read the original start only when this decision needs it."""
        return self.turn_record.read().started_by(updated_at_ms)

    @property
    def executing(self) -> bool:
        return bool(self.turn_record.items_with(Executing, self))

    def to_wire(self) -> dict[str, object]:
        """Schema-derived projection at a JSON boundary, not a hand-maintained mirror."""
        return FieldCodec.encode(self)


class NativeThread(Thread):
    __slots__ = ()
    required_refusal = "This participant checks its CLI inbox; it has no native prompt transport."

    def native_command_available(self, command: type[ThreadCommandAvailability],
                                 status: ThreadStatus) -> bool:
        return command.available_for(status, process=self.process_identity)

    def restart_candidates(self, status: ThreadStatus) -> tuple[Thread, ...]:
        if self.process_identity.runs_here():
            return ()
        return status.restart_candidates(self)

    def prepare_conversation[T](self, preparation: ConversationPreparation[T]) -> T:
        return preparation.native()

    def owner_binding(self, snapshot: ThreadOwnerSource[Thread]) -> ThreadOwnerBinding:
        return snapshot.status(self.name).native_owner_binding(snapshot, self)

    def presentation(self, status: ThreadStatus, ordinary: ThreadPresentation,
                     binding: ThreadOwnerBinding) -> ThreadPresentation:
        return status.native_presentation(self.process_identity, ordinary, binding)

    def project_activity(self, activity: ObservedActivity,
                         turns: Mapping[StoredTurnRecord, TurnDeclaration]) -> ObservedActivity:
        """A running native owner displays its current turn phase."""
        return self.turn_record.read_in(turns).project_activity(activity)


class ExternalThread(Thread):
    __slots__ = ()
    required_refusal = "Only an external CLI can publish a bridge process."

    def withdrawn(self) -> Thread:
        """The CLI bridge owns its binding and releases it as that process leaves
        (Registration.external_process); unregistering never frees the endpoint for a
        second bridge while the first still reads."""
        return self

    def serving_its_own_session(self) -> ExternalThread:
        """What this thread is once a process of its own serves it."""
        return self

    def stop_names(self, release_external: Callable[[str], None]) -> str | None:
        """No owner runtime serves an external endpoint (only RuntimeServer binds the socket a
        stop request dials, and no CLI bridge or inbox watch runs one), so fencing it would
        strand it: its holder's binding is released instead."""
        return self.process_identity.held_external_stop(
            lambda: release_external(self.name), self.name)

    def redeclared_child(self, execution: type[SubagentThread]) -> Thread:
        """A child that came to serve its own session keeps its name on its parent's grant;
        the caller's parent check, not the execution, says whose name it is."""
        return self

    def registered_status(self, requested: ThreadStatus) -> ThreadStatus:
        return requested.external_registered(self, time.time())

    def presentation(self, status: ThreadStatus, ordinary: ThreadPresentation,
                     binding: ThreadOwnerBinding) -> ThreadPresentation:
        return status.external_presentation(self.process_identity, ordinary)


class ServiceThread(NoChannelInbox, Thread):
    """A platform publisher has neither a prompt executor nor a CLI bridge."""

    __slots__ = ()

    def registered_status(self, requested: ThreadStatus) -> ThreadStatus:
        return ServiceThreadStatus()


class SubagentThread(ExternalThread, declared_name="subagent_execution"):
    """A child working inside its parent session's turn: it reads its CLI inbox, holds no
    order and has no independent card, and starts with inherited channel membership."""

    __slots__ = ()
    required_refusal = "This thread is not a declared subagent."

    def holds_activity(self, status: ThreadStatus) -> bool:
        """The occupied registration lasts until SubagentStop or SessionEnd retires it."""
        return status.is_receiving()

    def serving_its_own_session(self) -> ExternalThread:
        """A child works inside its parent's turn and never binds a process of its own, so one
        that does is an independent session: execution owns independence, and the parent stays
        as provenance. Connector names and hook forks both start here."""
        return ExternalThread(**{
            declared.name: getattr(self, declared.name)
            for declared in fields(self) if declared.init
        })

    def registered_status(self, requested: ThreadStatus) -> ThreadStatus:
        return requested

    def order_candidates(self) -> tuple[Thread, ...]:
        """A child works inside its parent's turn and never holds an order."""
        return ()

    def independent_threads(self) -> tuple[Thread, ...]:
        """A genuine in-turn child acts through its parent, not as another session."""
        return ()

    def teammate(self) -> str:
        """A genuine in-turn child works inside its parent's turn."""
        parent, = self.parent.names()
        return parent

    @classmethod
    def child_of(cls, parent: Thread, child: Thread) -> Thread:
        return replace(child, tags=DeclaredTag.for_child(parent.tags))

    @classmethod
    def child_goal(
        cls, goals: GoalFork[Thread, GoalDeclaration], parent: Thread,
    ) -> Callable[[Thread], GoalDeclaration]:
        return lambda prepared: prepared.goal



class ForkThread(NoChannelInbox, SubagentThread, declared_name="fork_execution"):
    """An SDK child borrows its bound parent's context and records its own fork goal.

    Any independent parent can declare it; the stored execution identity stays stable.
    """

    __slots__ = ()
    required_refusal = "This thread is not a declared teammate fork."

    def forks(self) -> tuple[Thread, ...]:
        return (self,)

    @classmethod
    def child_of(cls, parent: Thread, child: Thread) -> Thread:
        """A fork acts for its teammate, so it carries every tag the teammate carries,
        #teammates included: it is seen where its teammate is."""
        return replace(child, tags=parent.tags)

    @classmethod
    def child_goal(
        cls, goals: GoalFork[Thread, GoalDeclaration], parent: Thread,
    ) -> Callable[[Thread], GoalDeclaration]:
        return lambda prepared: goals.fork_goal(parent, prepared)



def current_thread(registry) -> Thread:
    """Declare the current thread from its owner launch environment."""
    launch = ThreadLaunch.current()
    return NativeThread(
        name=launch.name,
        tags=launch.tags,
        worktree=launch.worktree,
        parent=launch.parent,
        task=launch.task,
        process_identity=ProcessIdentity.capture(os.getpid()))


# Thread's backend field is the lookup boundary. Declare both implementations
# after the record graph, then reject incomplete declarations before any lookup.
# noqa: E402, F401
from agent_comms import pi_native_backend

# noqa: E402, F401
from agent_comms.execution import pi_durable_backend

AgentBackend.seal()
