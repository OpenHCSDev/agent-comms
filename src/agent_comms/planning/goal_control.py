"""Goal controls use the caller's captured authority, independent of its transport."""

from abc import ABC, abstractmethod

from agent_comms.planning.goals import Goal, GoalDeclaration


class GoalControlSource[Authority, Action](ABC):
    @abstractmethod
    async def observed_goal(self, authority: Authority) -> GoalDeclaration: ...

    @abstractmethod
    async def change_goal_control(
        self, authority: Authority, action: type[Action], goal: Goal,
    ) -> GoalDeclaration: ...

    @abstractmethod
    async def set_goal_control(self, authority: Authority, text: str) -> GoalDeclaration: ...

    @abstractmethod
    async def retry_goal_control(self, authority: Authority, goal: Goal) -> GoalDeclaration: ...
