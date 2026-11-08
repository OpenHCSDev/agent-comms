"""The original owner facts consumed by task scope decisions."""

from abc import ABC, abstractmethod

from .goals import GoalDeclaration
from .projects.checkouts import Checkout


class TaskScopeOwner[Scope](ABC):
    worktree: Checkout
    goal: GoalDeclaration

    @property
    @abstractmethod
    def task_scope(self) -> Scope: ...
