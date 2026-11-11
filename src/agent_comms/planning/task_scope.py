"""Original task scope decisions, below authored messages and thread assembly."""

from __future__ import annotations

from abc import abstractmethod
from collections.abc import Callable
from dataclasses import dataclass

from agent_comms.errors import RelationViolationError
from agent_comms.nominal.declared_family import DeclaredFamily
from agent_comms.planning.goals import GoalDeclaration, GoalRevision
from agent_comms.projects.checkouts import Checkout
from agent_comms.registry.task_scope_owner import TaskScopeOwner


@dataclass(frozen=True, kw_only=True)
class TaskScope(DeclaredFamily, affix="TaskScope"):
    project: Checkout

    @classmethod
    def for_goal(cls, project: Checkout, goal: GoalDeclaration) -> TaskScope:
        active = goal.active()
        scope: TaskScope = TurnTaskScope(project=project)
        return active.task_scope(scope, scope.with_goal)

    def with_goal(self, goal: GoalRevision) -> TaskScope:
        return GoalTaskScope(project=self.project, goal=goal)

    def require_current(self, owner: TaskScopeOwner[TaskScope]) -> None:
        if self.project != owner.worktree:
            raise RelationViolationError("Task project differs from its declared owner")
        self.require_context(owner)

    def require_human_context(self, owner: TaskScopeOwner[TaskScope]) -> None:
        self.require_current(owner)

    def for_human(self) -> TaskScope:
        return self

    @abstractmethod
    def require_context(self, owner: TaskScopeOwner[TaskScope]) -> None: ...

    @abstractmethod
    def applies(self, owner: TaskScopeOwner[TaskScope], turn_scope_matches: Callable[[], bool]) -> bool: ...

    def require_correction(
        self, previous_scope: TaskScope, require_same_turn: Callable[[], None],
    ) -> None:
        if previous_scope != self:
            raise RelationViolationError("Decision correction must preserve its original scope")


@dataclass(frozen=True, kw_only=True)
class GoalTaskScope(TaskScope):
    goal: GoalRevision

    def require_context(self, owner: TaskScopeOwner[TaskScope]) -> None:
        owner.goal.require().require_id(self.goal.id).require_active()
        owner.goal.require_checkpoint(self.goal)

    def applies(self, owner: TaskScopeOwner[TaskScope], turn_scope_matches: Callable[[], bool]) -> bool:
        return owner.task_scope == self


@dataclass(frozen=True, kw_only=True)
class ProjectTaskScope(TaskScope):
    def require_context(self, owner: TaskScopeOwner[TaskScope]) -> None:
        pass

    def applies(self, owner: TaskScopeOwner[TaskScope], turn_scope_matches: Callable[[], bool]) -> bool:
        return owner.worktree == self.project


@dataclass(frozen=True, kw_only=True)
class TurnTaskScope(TaskScope):
    """The enclosing declaration's original turn determines this scope."""

    def require_context(self, owner: TaskScopeOwner[TaskScope]) -> None:
        if owner.task_scope != self:
            raise RelationViolationError("An active goal requires its revision scope")

    def applies(self, owner: TaskScopeOwner[TaskScope], turn_scope_matches: Callable[[], bool]) -> bool:
        return owner.worktree == self.project and turn_scope_matches()

    def require_human_context(self, owner: TaskScopeOwner[TaskScope]) -> None:
        raise RelationViolationError("A USER constraint cannot borrow a model turn scope")

    def for_human(self) -> TaskScope:
        return ProjectTaskScope(project=self.project)

    def require_correction(
        self, previous_scope: TaskScope, require_same_turn: Callable[[], None],
    ) -> None:
        super().require_correction(previous_scope, require_same_turn)
        require_same_turn()


class TaskScopeSelection(DeclaredFamily, affix="ScopeSelection"):
    @abstractmethod
    def select(self, owner: TaskScopeOwner[TaskScope]) -> TaskScope: ...

    @abstractmethod
    def select_human(self, owner: TaskScopeOwner[TaskScope]) -> TaskScope: ...


@dataclass(frozen=True)
class CurrentTaskScopeSelection(TaskScopeSelection, declared_name="current"):
    def select(self, owner: TaskScopeOwner[TaskScope]) -> TaskScope:
        return owner.task_scope

    def select_human(self, owner: TaskScopeOwner[TaskScope]) -> TaskScope:
        return owner.task_scope.for_human()


@dataclass(frozen=True)
class ExplicitTaskScopeSelection(TaskScopeSelection, declared_name="explicit"):
    scope: TaskScope

    def select(self, owner: TaskScopeOwner[TaskScope]) -> TaskScope:
        self.scope.require_current(owner)
        return self.scope

    def select_human(self, owner: TaskScopeOwner[TaskScope]) -> TaskScope:
        self.scope.require_human_context(owner)
        return self.scope
