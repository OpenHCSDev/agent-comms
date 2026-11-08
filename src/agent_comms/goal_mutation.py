"""Goal mutation contracts, independent of command and runtime infrastructure."""

from abc import ABC, abstractmethod


class GoalMutation[Subject, Context, Result](ABC):
    @abstractmethod
    def change(self, goal: Subject, ctx: Context) -> Result: ...

    @abstractmethod
    def create(self, ctx: Context) -> Result: ...


class GoalStateOrder[Subject, Context](ABC):
    @abstractmethod
    def continue_goal(self, goal: Subject, ctx: Context) -> Subject: ...

    @abstractmethod
    def reactivate_goal(self, goal: Subject, ctx: Context) -> Subject: ...

    @abstractmethod
    def replace_goal(self, goal: Subject, ctx: Context) -> Subject: ...


class GoalStateProjection[Execution, Pause](ABC):
    @abstractmethod
    def active(self) -> Execution: ...

    @abstractmethod
    def paused(self, source: Pause) -> Execution: ...

    @abstractmethod
    def blocked(self, reason: str) -> Execution: ...

    @abstractmethod
    def completed(self) -> Execution: ...


class GoalFork[Owner, Declaration](ABC):
    @abstractmethod
    def fork_goal(self, parent: Owner, child: Owner) -> Declaration: ...
