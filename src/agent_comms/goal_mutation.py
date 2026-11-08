"""Goal mutation contracts, independent of command and runtime infrastructure."""

from abc import ABC, abstractmethod


class GoalMutation[Subject, Context, Result](ABC):
    @abstractmethod
    def change(self, goal: Subject, ctx: Context) -> Result: ...

    @abstractmethod
    def create(self, ctx: Context) -> Result: ...


class GoalFork[Owner, Declaration](ABC):
    @abstractmethod
    def fork_goal(self, parent: Owner, child: Owner) -> Declaration: ...
