"""The original owner facts consumed by task scope decisions."""

from abc import ABC, abstractmethod

from agent_comms.planning.goals import GoalDeclaration
from agent_comms.projects.checkouts import Checkout


class TaskScopeOwner[Scope](ABC):
    worktree: Checkout
    goal: GoalDeclaration

    @property
    @abstractmethod
    def task_scope(self) -> Scope: ...
