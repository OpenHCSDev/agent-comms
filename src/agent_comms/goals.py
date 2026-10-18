"""Goals: declaration and persistence owners."""

from __future__ import annotations

import re
from abc import abstractmethod
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import TYPE_CHECKING, ClassVar, Literal, TypeGuard

from .child_process import ExitedOutcome
from .declared_family import DeclaredFamily
from .errors import RelationViolationError
from .field_codec import FieldCodec, MalformedRecord, NullMember, RecordMember, SubclassUnion
from .goal_states import ActiveGoal, GoalState
from .registration_inheritance import RegistrationInheritance
from .thread_identity import RecordedThreadIncarnation, ThreadIncarnation
from .ui_model.goal import GoalDisplay

if TYPE_CHECKING:
    from .goal_presentation import GoalExecution, ThreadGoalExecution

if TYPE_CHECKING:
    from .goal_management import Goals
    from .todos import Todo


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
        from .relationships import GoalMentionDiagnostic

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
        from .relationships import GoalDerivedContact, GoalMentionDiagnostic

        diagnostic = GoalMentionDiagnostic(
            owner.name, goal.id, source.text_revision, self.token, "stale_incarnation"
        )
        if not self.peer.current(registry):
            return (), (diagnostic,)
        peer = registry.threads[self.peer.resolved(registry).name]
        contacts = tuple(
            GoalDerivedContact(
                owner.name,
                owner.created_at,
                executor.name,
                executor.created_at,
                goal.id,
                source.text_revision,
            )
            for executor in peer.role.executor(peer)
            if executor.created_at != owner.created_at
        )
        return (contacts, ()) if contacts else ((), (diagnostic,))


@dataclass(frozen=True, slots=True)
class GoalMentionSource:
    """Text-revision and owner-incarnation proof saved with its registry Goal."""

    goal_id: str
    text_revision: int
    text_digest: str
    owner_name: str
    owner_created_at: float
    bindings: tuple[GoalMentionBinding, ...]

    def __post_init__(self) -> None:
        from .text_digest import TextDigest

        GoalRevision(self.goal_id, self.text_revision)
        TextDigest(self.text_digest)
        RecordedThreadIncarnation(self.owner_name, self.owner_created_at)

    @property
    def checkpoint(self) -> GoalRevision:
        return GoalRevision(self.goal_id, self.text_revision)

    @property
    def digest(self):
        from .text_digest import TextDigest

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
        except MalformedRecord as error:
            raise ValueError(
                "Goal revision must be an exact nonnegative 63-bit integer."
            ) from error
        if not 0 <= revision < 1 << 63:
            raise ValueError("Goal revision must be an exact nonnegative 63-bit integer.")


class GoalCheckpoint(DeclaredFamily, affix="GoalCheckpoint"):
    """Captured goal identity, not a goal record or execution permission."""

    @abstractmethod
    def require_revision(self) -> GoalRevision: ...

    @abstractmethod
    def require_identity(self, goal_id: str) -> None: ...


@dataclass(frozen=True)
class AbsentGoalCheckpoint(GoalCheckpoint, declared_name="absent"):
    def require_revision(self) -> GoalRevision:
        raise RelationViolationError("No goal is set for this thread.")

    def require_identity(self, goal_id: str) -> None:
        raise ValueError("Goal execution identity does not match its declaration")


@dataclass(frozen=True)
class PresentGoalCheckpoint(GoalCheckpoint, declared_name="present"):
    revision: GoalRevision

    def require_revision(self) -> GoalRevision:
        return self.revision

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

    @staticmethod
    def printed(stdout: bytes) -> int | None:
        """The count a check printed (its last integer), or None when it printed no integer."""
        counts = re.findall(r"\d+", stdout.decode(errors="replace"))
        return int(counts[-1]) if counts else None

    def require(self, result):
        if not isinstance(result.outcome, ExitedOutcome) or self.printed(result.stdout) != self.expected:
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

    LINE: ClassVar[re.Pattern] = re.compile(r"(\S+)\s*::\s*(.+?)\s*->\s*(\d+)")

    @classmethod
    def parse(cls, line: str) -> OrderCheck | None:
        """The one reading of a check line, `<repo> :: <shell command> -> <integer>`; else None."""
        found = cls.LINE.fullmatch(line.strip())
        return found and cls(found[1], found[2], CountCheckResult(int(found[3])))

class GoalOrder(DeclaredFamily, affix="GoalOrder"):
    def require_completion(self, goal: Goal, goals: Goals, owner: ThreadIncarnation) -> None:
        pass

    def require_request(self) -> None:
        raise ValueError("An order serves an open owner request: request add records one.")

    def serving(
        self, goal: Goal, goals: Goals, owner: ThreadIncarnation
    ) -> tuple[Todo, ...] | None:
        """The undelivered orders serving this Goal as an owner request; None if it is none."""
        return None

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
class RequestGoalOrder(GoalOrder):
    """An owner request on the commander's own Goal: each order names the one it serves, and the
    request stays open until no order serving it is open."""

    def require_order(self, goal: Goal, goals: Goals, owner: ThreadIncarnation) -> None:
        pass

    def require_replacement(self, goal: Goal, goals: Goals, owner: ThreadIncarnation) -> None:
        if not goal.state.terminal:
            raise ValueError(f"Request {goal.id} is still open; mark it done first.")

    def require_completion(self, goal: Goal, goals: Goals, owner: ThreadIncarnation) -> None:
        if self.serving(goal, goals, owner):
            raise ValueError(f"Request {goal.id} still has open orders.")

    def require_request(self) -> None:
        pass

    def serving(self, goal: Goal, goals: Goals, owner: ThreadIncarnation) -> tuple[Todo, ...]:
        return tuple(todo for todo in goals.todos.for_request(owner, goal.id) if todo.pending)


@dataclass(frozen=True)
class ProblemGoalOrder(GoalOrder):
    """A unit's one Goal groups its canonical Todo assignments, not copied orders."""

    def require_completion(self, goal: Goal, goals: Goals, owner: ThreadIncarnation) -> None:
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


class GoalDeclaration(GoalDisplay, SubclassUnion, RegistrationInheritance["GoalDeclaration"]):
    """The recorded goal state, including a thread with no goal."""

    @classmethod
    def apply(cls, incoming: GoalDeclaration, previous: GoalDeclaration) -> GoalDeclaration:
        return incoming.registration_value(previous)

    def registration_value(self, previous: GoalDeclaration) -> GoalDeclaration:
        return previous

    def check_expected(self, current: GoalDeclaration) -> None:
        pass

    def accepts_following_handoff(self) -> bool:
        return False

    def defers_following_to(self, current: GoalDeclaration) -> bool:
        return current.accepts_following_handoff()

    @property
    def snapshot(self) -> GoalDeclaration:
        return self

    @property
    @abstractmethod
    def shows_document(self) -> bool: ...

    @property
    @abstractmethod
    def shows_progress(self) -> bool: ...

    @property
    @abstractmethod
    def objective(self) -> str: ...

    @property
    @abstractmethod
    def progress(self) -> str: ...

    def unavailable_heading(self) -> str:
        return "Goal state unavailable"

    def task_scope(self, project):
        from .task_sources import TurnTaskScope
        return TurnTaskScope(project=project)

    def instructions_for(self, goals: Goals, owner: ThreadIncarnation) -> str:
        return ""

    def context_segments(self, thread, goals):
        return ()

    def task_facts(self):
        return ()

    def wake_context(self):
        return None

    def continued_by(self, current: GoalDeclaration) -> GoalDeclaration:
        return self

    def require_replacement(self, goals, owner) -> None:
        pass

    def require(self) -> Goal:
        raise RelationViolationError("No goal is set for this thread.")

    @abstractmethod
    def require_id(self, goal_id: str) -> Goal: ...

    @abstractmethod
    def reported_id(self) -> str | None: ...

    @abstractmethod
    def require_state(self, expected: GoalState) -> None: ...

    @abstractmethod
    def wait(self, waits): ...

    @abstractmethod
    def clear_wait(self, waits, *, wait_id: str | None = None) -> bool: ...

    def require_active(self) -> Goal:
        goal = self.require()
        goal.state.require_active()
        return goal

    def require_checkpoint(self, checkpoint: GoalRevision) -> Goal:
        if self.captured != PresentGoalCheckpoint(checkpoint):
            raise ValueError("The goal changed before this action; the action was not applied.")
        return self.require()

    def matching(self, goal_id: str) -> GoalDeclaration:
        return self

    def active(self) -> GoalDeclaration:
        return self

    def accepts_observation(self, goal_id: str, minimum_revision: int) -> bool:
        return False

    def reports_turn(self, turn_id: str) -> bool:
        return False

    @property
    def captured(self) -> GoalCheckpoint:
        return AbsentGoalCheckpoint()


@dataclass(frozen=True)
class NoGoal(NullMember, GoalDeclaration):
    def require_id(self, goal_id: str) -> Goal:
        raise RelationViolationError("This goal was replaced or cleared; refresh its state.")

    def reported_id(self) -> None:
        return None

    def require_state(self, expected: GoalState) -> None:
        raise ValueError("This goal is no longer active; refresh its state.")

    def wait(self, waits):
        from .goal_waits import NoGoalWait
        return NoGoalWait()

    def clear_wait(self, waits, *, wait_id: str | None = None) -> bool:
        return False

    @property
    def can_control(self) -> bool:
        return False

    @property
    def visible(self) -> bool:
        return False

    @property
    def shows_document(self) -> bool:
        return False

    @property
    def shows_progress(self) -> bool:
        return False

    @property
    def objective(self) -> str:
        return ""

    @property
    def progress(self) -> str:
        return ""

    def standby(self, execution: ThreadGoalExecution) -> TypeGuard[GoalExecution]:
        return False

    def heading(self, execution: ThreadGoalExecution) -> str:
        return "No current goal"

    def action_failure(self, error: Exception) -> str:
        return "This goal has been cleared."


@dataclass(frozen=True)
class Goal(RecordMember, GoalDeclaration):
    """A goal owns one typed record for storage and agent-comms protocol boundaries."""

    text: str
    id: str
    published_seq: int
    progress: str = ""
    revision: int = 0
    reported_turn: str | None = None
    mention_source: GoalMentionSource | None = None
    state: GoalState = field(default_factory=ActiveGoal)
    order: GoalOrder = field(default_factory=UnorderedGoalOrder)

    def registration_value(self, previous: GoalDeclaration) -> GoalDeclaration:
        return self

    def check_expected(self, current: GoalDeclaration) -> None:
        if self != current:
            raise ValueError("Goal changed during resume; refresh its state.")

    def accepts_following_handoff(self) -> bool:
        return self.state.active

    def defers_following_to(self, current: GoalDeclaration) -> bool:
        return not self.state.active and super().defers_following_to(current)

    @property
    def shows_document(self) -> bool:
        return True

    @property
    def shows_progress(self) -> bool:
        return self.progress != Goal.progress

    @property
    def objective(self) -> str:
        return self.text

    def details_heading(self, execution: ThreadGoalExecution) -> str:
        heading = self.heading(execution)
        if self.standby(execution):
            return f"{heading} · {execution.state.view.standby_detail(execution)}"
        return heading

    @property
    def can_control(self) -> bool:
        return True

    @property
    def visible(self) -> bool:
        return True

    def standby(self, execution: ThreadGoalExecution) -> TypeGuard[GoalExecution]:
        return execution.waiting_for(self)

    def heading(self, execution: ThreadGoalExecution) -> str:
        status = "Standby" if self.standby(execution) else self.state.declared_name
        return f"Goal · {status} · rev {self.revision}"

    def action_failure(self, error: Exception) -> str:
        return str(error)

    def unavailable_heading(self) -> str:
        return "Goal state unavailable · last confirmed snapshot"

    def task_scope(self, project):
        from .task_sources import GoalTaskScope
        return GoalTaskScope(project=project, goal=self.checkpoint)

    def instructions_for(self, goals: Goals, owner: ThreadIncarnation) -> str:
        return self.instructions(self.order.todos(goals, owner, self.id))

    def context_segments(self, thread, goals):
        from .turn_context import GoalSegment
        return (GoalSegment.capture(thread, self, goals),)

    def task_facts(self):
        from .retained_task_facts import GoalTaskFact
        return (GoalTaskFact(self),)

    def wake_context(self):
        return {"text": self.text, "status": self.state.declared_name, "progress": self.progress}

    def continued_by(self, current: GoalDeclaration) -> GoalDeclaration:
        return current.matching(self.id)

    def require(self) -> Goal:
        return self

    def require_id(self, goal_id: str) -> Goal:
        if self.id != goal_id:
            raise RelationViolationError("This goal was replaced or cleared; refresh its state.")
        return self

    def reported_id(self) -> str:
        return self.id

    def require_state(self, expected: GoalState) -> None:
        if self.state != expected:
            raise ValueError(self.state.activity_refusal())

    def wait(self, waits):
        return waits.for_goal(self, waits.read())

    def clear_wait(self, waits, *, wait_id: str | None = None) -> bool:
        return waits.clear(self.id, wait_id=wait_id)

    def matching(self, goal_id: str) -> GoalDeclaration:
        return self if self.id == goal_id else NoGoal()

    def active(self) -> GoalDeclaration:
        return self if self.state.active else NoGoal()

    def reports_turn(self, turn_id: str) -> bool:
        return self.reported_turn == turn_id

    @property
    def captured(self) -> GoalCheckpoint:
        return PresentGoalCheckpoint(self.checkpoint)

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

    @property
    def summary(self) -> str:
        return f"Goal · {self.state.declared_name}: {self.text}"
