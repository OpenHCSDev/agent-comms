"""Goal controls use the caller's captured authority, independent of its transport."""

from abc import ABC, abstractmethod



class GoalControlSource[Authority, Action, Subject, Declaration](ABC):
    @abstractmethod
    async def observed_goal(self, authority: Authority) -> Declaration: ...

    @abstractmethod
    async def change_goal_control(
        self, authority: Authority, action: type[Action], goal: Subject,
    ) -> Declaration: ...

    @abstractmethod
    async def set_goal_control(self, authority: Authority, text: str) -> Declaration: ...

    @abstractmethod
    async def retry_goal_control(self, authority: Authority, goal: Subject) -> Declaration: ...
