"""Goals: declaration and persistence owners."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from pathlib import Path
from functools import partial
from typing import ClassVar, Literal

from abc import (
    ABC,
    abstractmethod,
)
import re

from .child_process import ExitedOutcome

from .declared_family import DeclaredFamily
from .thread_identity import RecordedThreadIncarnation, ThreadIncarnation
from .thread_relations import GoalDerivedContact
from .text_digest import TextDigest
from .ui_model.goal import NoGoal as NoGoalDisplay, ShowingGoal

from .field_codec import FieldCodec, WireValue
from .errors import RelationViolationError
from .goal_states import ActiveGoal, GoalState
from .goal_attempt_identity import FailureNotObserved
from .goal_presentation import GoalExecution, GoalExecutionState, IdleGoalExecution, ThreadGoalExecution


@dataclass(frozen=True, kw_only=True)
class GoalMentionBinding(DeclaredFamily, affix="MentionBinding"):
    """One current stored resolution family; each member owns its projection."""

    family_discriminator = "resolution"
    token: str

    def __post_init__(self):
        if not self.token:
            raise ValueError("Goal mention requires a token")

    @abstractmethod
    def project(self, registry, owner, goal, source): ...


@dataclass(frozen=True, kw_only=True)
class UnresolvedMention:
    """Shared diagnostic capability; never a separately registered state."""

    peer_name: Literal[None] = None
    peer_created_at: Literal[None] = None

    def project(self, registry, owner, goal, source):
        return (), (
            GoalMentionDiagnostic(
                owner.name, goal.id, source.text_revision, self.token, type(self)
            ),
        )


@dataclass(frozen=True, kw_only=True)
class SelfMentionBinding(UnresolvedMention, GoalMentionBinding):
    pass


@dataclass(frozen=True, kw_only=True)
class UnknownMentionBinding(UnresolvedMention, GoalMentionBinding):
    pass


@dataclass(frozen=True, kw_only=True)
class AliasMentionBinding(UnresolvedMention, GoalMentionBinding):
    pass


@dataclass(frozen=True, kw_only=True)
class MalformedMentionBinding(UnresolvedMention, GoalMentionBinding):
    pass


@dataclass(frozen=True, kw_only=True)
class LimitExceededMentionBinding(UnresolvedMention, GoalMentionBinding):
    pass


@dataclass(frozen=True, kw_only=True)
class NonExecutableMentionBinding(UnresolvedMention, GoalMentionBinding):
    pass


@dataclass(frozen=True, kw_only=True)
class ResolvedMentionBinding(GoalMentionBinding):
    peer_name: str
    peer_created_at: float

    def __post_init__(self):
        super().__post_init__()
        RecordedThreadIncarnation(self.peer_name, self.peer_created_at)

    @property
    def peer(self) -> ThreadIncarnation:
        return RecordedThreadIncarnation(self.peer_name, self.peer_created_at)

    def project(self, registry, owner, goal, source):
        diagnostic = GoalMentionDiagnostic(
            owner.name, goal.id, source.text_revision, self.token, "stale_incarnation"
        )
        if not self.peer.current(registry):
            return (), (diagnostic,)
        peer = registry.threads[self.peer.resolved(registry).name]
        try:
            peer.role.require_executable()
        except RelationViolationError:
            return (), (diagnostic,)
        if peer.created_at == owner.created_at:
            return (), (diagnostic,)
        return (
            GoalDerivedContact(
                owner.name,
                owner.created_at,
                peer.name,
                peer.created_at,
                goal.id,
                source.text_revision,
            ),
        ), ()


@dataclass(frozen=True, slots=True)
class GoalMentionDiagnostic:
    owner: str
    goal_id: str
    text_revision: int
    token: str
    reason: type[GoalMentionBinding] | Literal["stale_incarnation"]


class GoalMentionEvidence(WireValue):
    accepts_null = True

    @classmethod
    def from_wire(cls, data):
        return NoMentionSource() if data is None else FieldCodec.decode_record(GoalMentionSource, data)

    @classmethod
    def schema(cls):
        return {"anyOf": [{"type": "null"}, FieldCodec.record_schema(GoalMentionSource)]}

    def matches(self, goal, owner, registry) -> bool:
        return False

    def contacts(self, goal, owner, registry):
        return (), ()


@dataclass(frozen=True)
class NoMentionSource(GoalMentionEvidence):
    def to_wire(self):
        return None


@dataclass(frozen=True, slots=True)
class GoalMentionSource(GoalMentionEvidence):
    """Text-revision and owner-incarnation proof saved with its registry Goal."""

    goal_id: str
    text_revision: int
    text_digest: str
    owner_name: str
    owner_created_at: float
    bindings: tuple[GoalMentionBinding, ...]
    accepts_null = False

    def to_wire(self):
        return FieldCodec.encode_record(self)

    def contacts(self, goal, owner, registry):
        contacts, diagnostics = [], []
        if owner.role.executable and self.matches(goal, owner, registry):
            for binding in self.bindings:
                projected, issues = binding.project(registry, owner, goal, self)
                contacts.extend(projected)
                diagnostics.extend(issues)
        return tuple(contacts), tuple(diagnostics)

    def __post_init__(self) -> None:
        GoalRevision(self.goal_id, self.text_revision)
        TextDigest(self.text_digest)
        RecordedThreadIncarnation(self.owner_name, self.owner_created_at)

    @property
    def checkpoint(self) -> GoalRevision:
        return GoalRevision(self.goal_id, self.text_revision)

    @property
    def digest(self):
        return TextDigest(self.text_digest)

    @property
    def incarnation(self) -> ThreadIncarnation:
        return RecordedThreadIncarnation(self.owner_name, self.owner_created_at)

    def matches(self, goal, owner, registry) -> bool:
        if self.goal_id != goal.id or self.text_revision > goal.revision:
            return False
        return (
            self.digest.matches(goal.text)
            and self.incarnation.resolved(registry) == owner.incarnation
        )


@dataclass(frozen=True)
class GoalRevision:
    """A particular revision, independent of text and execution state."""

    id: str
    revision: int

    def __post_init__(self) -> None:
        if not FieldCodec.decode(str, self.id):
            raise ValueError("Goal revision requires identity")
        try:
            revision = FieldCodec.decode(int, self.revision)
        except (TypeError, ValueError) as error:
            raise ValueError(
                "Goal revision must be an exact nonnegative 63-bit integer."
            ) from error
        if not 0 <= revision < 1 << 63:
            raise ValueError("Goal revision must be an exact nonnegative 63-bit integer.")


class GoalCheckpoint(DeclaredFamily, affix="GoalCheckpoint"):
    """Captured goal identity, not a goal record or execution permission."""

    @abstractmethod
    def require_identity(self, goal_id: str) -> None: ...


@dataclass(frozen=True)
class AbsentGoalCheckpoint(GoalCheckpoint, declared_name="absent"):
    def require_identity(self, goal_id: str) -> None:
        raise ValueError("Goal execution identity does not match its declaration")


@dataclass(frozen=True)
class PresentGoalCheckpoint(GoalCheckpoint, declared_name="present"):
    revision: GoalRevision

    def require_identity(self, goal_id: str) -> None:
        if self.revision.id != goal_id:
            raise ValueError("Goal execution identity does not match its declaration")


class CheckResult(DeclaredFamily, affix="CheckResult"):
    @abstractmethod
    def require(self, result) -> None: ...


@dataclass(frozen=True)
class SilentCheckResult(CheckResult):
    def require(self, result):
        if result.outcome != ExitedOutcome(0) or result.stdout:
            raise ValueError("The order check must print nothing and exit zero.")


@dataclass(frozen=True)
class CountCheckResult(CheckResult):
    expected: int

    def __post_init__(self):
        if self.expected < 0:
            raise ValueError("An expected count must be nonnegative.")

    def require(self, result):
        counts = re.findall(r"\d+", result.stdout.decode(errors="replace"))
        if not isinstance(result.outcome, ExitedOutcome) or not counts or int(counts[-1]) != self.expected:
            raise ValueError(f"The order check did not print its expected last integer {self.expected}.")


@dataclass(frozen=True)
class OrderCheck:
    directory: str
    command: str
    result: CheckResult = field(default_factory=SilentCheckResult)

    def __post_init__(self):
        if not self.directory.strip() or not self.command.strip():
            raise ValueError("An order check requires a directory and command.")

    def require_directory(self):
        if not Path(self.directory).is_dir():
            raise ValueError("The order check directory does not exist.")

class GoalOrder(DeclaredFamily, affix="GoalOrder"):
    def require_slot(self, goal, goal_id, problem):
        if goal.id != goal_id:
            goal.state.require_order_slot()

    def require_completion(self) -> None:
        pass

    def instructions(self, todos) -> str:
        return ""

    def todos(self, goals, owner, goal_id):
        return ()

    @abstractmethod
    def require_order(self, goal, goals, owner) -> None: ...

    @abstractmethod
    def require_replacement(self, goal, goals, owner) -> None: ...


@dataclass(frozen=True)
class UnorderedGoalOrder(GoalOrder):
    def require_order(self, goal, goals, owner) -> None:
        pass

    def require_replacement(self, goal, goals, owner) -> None:
        pass


@dataclass(frozen=True)
class ProblemGoalOrder(GoalOrder):
    """A unit's one Goal groups its canonical Todo assignments, not copied orders."""

    def require_completion(self):
        raise ValueError("Complete each problem with its checked delivered commit.")

    def require_order(self, goal, goals, owner):
        if not goals.todos.for_goal(owner, goal.id):
            raise ValueError("The problem Goal has no assigned Todos.")

    def require_replacement(self, goal, goals, owner):
        if any(todo.pending for todo in goals.todos.for_goal(owner, goal.id)):
            raise ValueError("Finish or release the assigned problems before replacing this Goal.")

    def todos(self, goals, owner, goal_id):
        return goals.todos.for_goal(owner, goal_id)

    def instructions(self, todos):
        return "\n\n".join(todo.order.instructions(todo) for todo in todos if todo.pending)


class GoalDeclaration(WireValue):
    """A thread's goal declaration; null is only its external representation."""

    accepts_null = True

    @classmethod
    def from_wire(cls, data):
        return NoGoal() if data is None else FieldCodec.decode_record(Goal, data)

    @classmethod
    def schema(cls):
        return {"anyOf": [{"type": "null"}, FieldCodec.record_schema(Goal)]}

    def require(self) -> Goal:
        raise RelationViolationError("No goal is set for this thread.")

    def require_id(self, goal_id: str) -> Goal:
        return self.matching(goal_id).require_selection()

    def require_selection(self) -> Goal:
        raise RelationViolationError("This goal was replaced or cleared; refresh its state.")

    def require_model_resume(self) -> Goal:
        raise ValueError("This goal cannot be resumed; refresh its state.")

    def require_active(self) -> Goal:
        goal = self.require()
        goal.state.require_active()
        return goal

    def require_checkpoint(self, checkpoint: GoalRevision) -> Goal:
        if self.captured != PresentGoalCheckpoint(checkpoint):
            raise ValueError("The goal changed before this action; the action was not applied.")
        return self.require()

    def require_replacement(self, goals, owner) -> None:
        pass

    def require_order_slot(self, goal_id: str, problem: str) -> None:
        pass

    def require_state(self, state):
        raise ValueError("This goal is no longer active; refresh its state.")

    def accepts_observation(self, goal_id: str, minimum_revision: int) -> bool:
        return False

    def reports_turn(self, turn_id: str) -> bool:
        return False

    def capture_baseline(self, history, owner_created_at: float) -> None:
        pass

    def failure_projection(self, reason):
        raise FailureNotObserved("owner_or_goal_changed")

    def matching(self, goal_id: str) -> GoalDeclaration:
        return self

    def active(self) -> GoalDeclaration:
        return self

    def continuation(self, current: GoalDeclaration) -> GoalDeclaration:
        return self

    @property
    def captured(self) -> GoalCheckpoint:
        return AbsentGoalCheckpoint()

    def wake_context(self):
        return self

    def owes_project_resume(self, thread) -> bool:
        return thread.project_resume_due and not thread.executing

    def active_goals(self) -> tuple[Goal, ...]:
        return ()

    def permits_continuation(self, goal_id: str) -> bool:
        return self.active().continues(goal_id)

    def continues(self, goal_id: str) -> bool:
        return False

    def permits_inactive_input(self) -> bool:
        return True

    def permits_accepted(self, goal_id: str | None) -> bool:
        return goal_id is None

    def accepted_goal_id(self) -> str | None:
        return None

    def defers_following_from(self, original: GoalDeclaration) -> bool:
        return False

    def display(self):
        return NoGoalDisplay()

    def task_scope(self, owner):
        return TurnTaskScope(project=owner.worktree)

    def turn_permission(self):
        return InactiveGoalPermission()

    def prepare_turn(self, turn, permits) -> bool:
        return not turn.autonomous_goal

    def filter_scheduled(self, pending):
        return pending

    async def failed_schedule(self, error, scheduled, comms, owner, worker):
        raise error

    def recorded_wait(self, rows):
        return NoGoalWait()

    def execution(self, rows, snapshot, agents):
        return IdleGoalExecution()

    def current_wait(self, rows, owner):
        return self.active().recorded_wait(rows).for_current(self, owner)

    def fail_origin(self, account):
        pass

    async def pause_for_cancel(self, runner, name):
        pass

    def contacts(self, owner, registry):
        return (), ()

    def peer_instructions(self, owner, goals) -> str | None:
        return None

    def apply_set_goal(self, action, ctx):
        return action.create_goal(ctx)

    def apply_problem_order(self, action, ctx):
        return action.create_goal(ctx)

    def append_progress(self, note: str) -> GoalDeclaration:
        return self

    def block_after_failed(self, goals, owner, diagnostic: str):
        return owner.goal

    def record_usage(self, account, event):
        account.unattributed_usage.append((str(event.response_id), event.usage))

    def claim_origin(self, account, owner, completions):
        pass

    def check_turn_result(self, account, event):
        pass

    def block_turn_account(self, account, diagnostic: str):
        pass

    def settle_turn(self, settlement, store, permit, turn):
        pass

    def registration_value(self, previous):
        return previous

    def clear_wait(self, waits, *, wait_id: str | None = None) -> bool:
        return False

    def retire_attempt(self, goals):
        pass



@dataclass(frozen=True)
class NoGoal(GoalDeclaration):
    def to_wire(self):
        return None


@dataclass(frozen=True)
class Goal(GoalDeclaration):
    """A goal owns one typed record for storage and agent-comms protocol boundaries."""

    text: str
    id: str
    published_seq: int
    progress: str = ""
    revision: int = 0
    reported_turn: str | None = None
    mention_source: GoalMentionEvidence = field(default_factory=NoMentionSource)
    state: GoalState = field(default_factory=ActiveGoal)
    order: GoalOrder = field(default_factory=UnorderedGoalOrder)

    accepts_null = False

    def require(self) -> Goal:
        return self

    def require_selection(self) -> Goal:
        return self

    def require_model_resume(self) -> Goal:
        self.state.require_model_resume()
        return self

    def matching(self, goal_id: str) -> GoalDeclaration:
        return self if self.id == goal_id else NoGoal()

    def active(self) -> GoalDeclaration:
        return self.state.declaration(self)

    def continuation(self, current: GoalDeclaration) -> GoalDeclaration:
        return current.matching(self.id)

    @property
    def captured(self) -> GoalCheckpoint:
        return PresentGoalCheckpoint(self.checkpoint)

    def require_state(self, state):
        if self.state != state:
            raise ValueError(self.state.activity_refusal())

    def failure_projection(self, reason):
        return self.state.failure_projection(reason)

    def require_order_slot(self, goal_id: str, problem: str) -> None:
        self.order.require_slot(self, goal_id, problem)

    def wake_context(self):
        return {"text": self.text, "status": self.state.declared_name, "progress": self.progress}

    def owes_project_resume(self, thread) -> bool:
        return False

    def active_goals(self) -> tuple[Goal, ...]:
        return (self,)

    def continues(self, goal_id: str) -> bool:
        return self.id == goal_id

    def permits_inactive_input(self) -> bool:
        return False

    def permits_accepted(self, goal_id: str | None) -> bool:
        return self.active().accepted_goal_id() == goal_id

    def accepted_goal_id(self) -> str:
        return self.id

    def defers_following_from(self, original: GoalDeclaration) -> bool:
        return original.permits_inactive_input()

    def recorded_wait(self, rows):
        return rows.get(self.id, NoGoalWait())

    def execution(self, rows, snapshot, agents):
        return self.active().recorded_wait(rows).project(self, snapshot, agents)

    def fail_origin(self, account):
        account.fail_origin(self)

    async def pause_for_cancel(self, runner, name):
        await runner.pause_goal(name, self.id)

    async def failed_schedule(self, error, scheduled, comms, owner, worker):
        scheduled.require_dependency_wake(error)
        await worker(partial(
            comms.goals.block_goal_after_failed_turn, owner.name,
            started_goal=self, expected_worktree=owner.worktree,
            diagnostic="Standby wake launch authority unavailable; inspect the UNKNOWN "
                       "input before explicit Retry. No input was replayed.",
        ))

    def display(self):
        return ShowingGoal(self)

    def task_scope(self, owner):
        return GoalTaskScope(project=owner.worktree, goal=self.checkpoint)

    def turn_permission(self):
        return ContinuationGoalPermission(self)

    def prepare_turn(self, turn, permits) -> bool:
        turn.goal_permit = turn.runner.comms.goals.reserve_turn(
            self, owner=turn.thread, autonomous=turn.autonomous_goal,
        )
        return turn.goal_permit.start(permits, turn.runner.comms.goals.open_goal_store())

    def filter_scheduled(self, pending):
        return [turn for turn in pending if turn.goal_id == self.id]

    def contacts(self, owner, registry):
        return self.mention_source.contacts(self, owner, registry)

    def peer_instructions(self, owner, goals) -> str:
        return self.instructions(self.order.todos(goals, owner.incarnation, self.id))

    def apply_set_goal(self, action, ctx):
        if (self.text, self.order) != (action.text.strip(), action.order):
            raise ValueError("Goal identity already has different instructions.")
        return self

    def apply_problem_order(self, action, ctx):
        return replace(self, order=ProblemGoalOrder(), revision=self.revision + 1)

    def append_progress(self, note: str) -> Goal:
        return replace(self, progress=f"{self.progress}\n\n{note}" if self.progress else note,
                       revision=self.revision + 1)

    def block_after_failed(self, goals, owner, diagnostic: str):
        blocked = self.after_failed_turn(diagnostic)
        if blocked is not self:
            goals.registry.register(replace(owner, goal=blocked), goals.registry.status(owner.name))
        return blocked

    def record_usage(self, account, event):
        account.record_goal_usage(self, event)

    def claim_origin(self, account, owner, completions):
        account.claim_goal_origin(self, owner, completions)

    def check_turn_result(self, account, event):
        if not event.ok:
            account.block_current("Backend turn failed; inspect local diagnostics before resuming.")
        elif account.productive_tool is None and not str(event.text or "").strip():
            account.block_current("Backend reported success without assistant output or tool activity; "
                                  "inspect the session before resuming.")

    def block_turn_account(self, account, diagnostic: str):
        account.block_current(diagnostic)

    def settle_turn(self, settlement, store, permit, turn):
        settlement(store, permit, self, turn).dispatch_sync(self.state)

    def registration_value(self, previous):
        return self

    def clear_wait(self, waits, *, wait_id: str | None = None) -> bool:
        return waits.clear(self.id, wait_id=wait_id)

    def retire_attempt(self, goals):
        attempts = goals.open_goal_store()
        generation = attempts.snapshot(self.id)
        if generation is not None and not generation.lifecycle.terminal:
            attempts.retire_goal(self.id, expected_generation=generation.number,
                                 attempt_id=generation.attempt_id)

    def instructions(self, todos) -> str:
        return self.render_instructions(self.order.instructions(todos))

    def render_instructions(self, orders_text: str) -> str:
        return "\n\n".join(part for part in (self.text, orders_text) if part)

    def require_order(self, goals, owner) -> None:
        self.order.require_order(self, goals, owner)

    def require_replacement(self, goals, owner) -> None:
        self.order.require_replacement(self, goals, owner)

    def after_failed_turn(self, diagnostic: str) -> Goal:
        progress = f"{self.progress}\n\n{diagnostic}" if self.progress else diagnostic
        return self._failure_projection(self.state.after_failed_turn(diagnostic), progress)

    def after_unverified_completion(self, diagnostic: str) -> Goal:
        return self._failure_projection(
            self.state.after_unverified_completion(diagnostic), diagnostic
        )

    def _failure_projection(self, state: GoalState, progress: str) -> Goal:
        if state is self.state:
            return self
        return replace(self, state=state, progress=progress, revision=self.revision + 1)

    def to_wire(self) -> dict[str, object]:
        return FieldCodec.encode_record(self)

    def __post_init__(self) -> None:
        if not self.text.strip() or not self.id:
            raise ValueError("A goal requires text and an identity.")
        GoalRevision(self.id, self.revision)
        if self.published_seq < 0:
            raise ValueError("Goal publication requires a nonnegative bus sequence.")
        if self.reported_turn is not None and not isinstance(self.reported_turn, str):
            raise ValueError("Goal reported turn must be a string or null.")

    @property
    def checkpoint(self) -> GoalRevision:
        return GoalRevision(self.id, self.revision)

    def accepts_observation(self, goal_id: str, minimum_revision: int) -> bool:
        """Later revisions may describe the same failed goal, never a replacement."""
        return self.id == goal_id and self.revision >= minimum_revision

    def reports_turn(self, turn_id: str) -> bool:
        return self.reported_turn == turn_id

    def capture_baseline(self, history, owner_created_at: float) -> None:
        history.baseline(owner_created_at, self)

    @property
    def summary(self) -> str:
        return f"Goal · {self.state.declared_name}: {self.text}"


@dataclass(frozen=True)
class GoalSnapshot:
    """A declaration and its execution read from the same owner snapshot."""

    goal: GoalDeclaration
    execution: ThreadGoalExecution

    def __post_init__(self):
        self.execution.validate_goal(self.goal.captured)


class GoalWait(ABC):
    """A resolved dependency state; only recorded waits are persisted."""

    @abstractmethod
    def allows_routed_input(self) -> bool: ...

    def matches_id(self, wait_id: str) -> bool:
        return False

    def owner_matches(self, owner) -> bool:
        return False

    def matches(self, original, owner) -> bool:
        return False

    def review_after_seq(self, goal, owner, targets) -> int:
        return goal.published_seq

    def consume_dependency(self, comms, canonical: str) -> bool:
        return True

    def consume_reply(self, waits, goal_id: str) -> bool:
        return False

    def for_current(self, goal, owner):
        return self

    def dependency_targets(self):
        return ()

    def belongs_to_turn(self, turn) -> bool:
        return False

    def include_dependency(self, pending, name) -> None:
        pass

    def release_closed(self, goals, owner, snapshot, rows):
        return ()

    def consume_owned_reply(self, goals, owner, reference) -> bool:
        return False

    def project(self, goal, snapshot, agents):
        return GoalExecution(GoalExecutionState.for_domain_state(goal.state), goal.id,
                             block_reason=goal.state.reason)


@dataclass(frozen=True, slots=True)
class NoGoalWait(GoalWait):
    def allows_routed_input(self) -> bool:
        return True


@dataclass(frozen=True, kw_only=True)
class TaskScope(DeclaredFamily, affix="TaskScope"):
    project: str

    def require_current(self, owner) -> None:
        if self.project != owner.worktree:
            raise RelationViolationError("Task project differs from its declared owner")
        self.require_context(owner)

    def require_human_context(self, owner) -> None:
        self.require_current(owner)

    def for_human(self):
        return self

    @abstractmethod
    def require_context(self, owner) -> None: ...

    @abstractmethod
    def applies(self, owner, declaration, registry) -> bool: ...

    def require_correction(self, previous, correction, registry) -> None:
        if previous.scope != self:
            raise RelationViolationError("Decision correction must preserve its original scope")


@dataclass(frozen=True, kw_only=True)
class GoalTaskScope(TaskScope):
    goal: GoalRevision

    def require_context(self, owner) -> None:
        owner.goal.require_id(self.goal.id).require_active()
        owner.goal.require_checkpoint(self.goal)

    def applies(self, owner, declaration, registry) -> bool:
        return owner.goal.active().task_scope(owner) == self


@dataclass(frozen=True, kw_only=True)
class ProjectTaskScope(TaskScope):
    def require_context(self, owner) -> None:
        pass

    def applies(self, owner, declaration, registry) -> bool:
        return owner.worktree == self.project


@dataclass(frozen=True, kw_only=True)
class TurnTaskScope(TaskScope):
    """The enclosing declaration's original turn determines this scope."""

    def require_context(self, owner) -> None:
        if owner.goal.active().task_scope(owner) != self:
            raise RelationViolationError("An active goal requires its revision scope")

    def applies(self, owner, declaration, registry) -> bool:
        return owner.worktree == self.project and declaration.turn_scope_matches(owner, registry)

    def require_human_context(self, owner) -> None:
        raise RelationViolationError("A USER constraint cannot borrow a model turn scope")

    def for_human(self):
        return ProjectTaskScope(project=self.project)

    def require_correction(self, previous, correction, registry) -> None:
        super().require_correction(previous, correction, registry)
        previous.require_same_turn(correction, registry)


class TurnGoalPermission(ABC):

    @abstractmethod
    def allows(self, current: GoalDeclaration) -> bool: ...


@dataclass(frozen=True)
class OwnerGoalPermission(TurnGoalPermission):
    goal: GoalDeclaration

    def allows(self, current: GoalDeclaration) -> bool:
        return current == self.goal


@dataclass(frozen=True)
class ContinuationGoalPermission(TurnGoalPermission):
    goal: Goal

    def allows(self, current: GoalDeclaration) -> bool:
        return current.permits_continuation(self.goal.id)


class InactiveGoalPermission(TurnGoalPermission):
    def allows(self, current: GoalDeclaration) -> bool:
        return current.active().permits_inactive_input()


@dataclass(frozen=True)
class AcceptedGoalPermission(TurnGoalPermission):
    goal_id: str | None

    def allows(self, current: GoalDeclaration) -> bool:
        return current.permits_accepted(self.goal_id)
