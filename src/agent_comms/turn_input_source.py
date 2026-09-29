"""Original and follow-up inputs own their captured goal/wait and display semantics."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import TYPE_CHECKING, ClassVar

from .messages import Message
from .turn_goal_permission import TurnGoalPermission

if TYPE_CHECKING:
    from .comms import Comms
    from .goal_waits import GoalWait
    from .goals import Goal
    from .input_disposition import InputDispositions
    from .input_drain import InputDrain
    from .registry_document import RegistrySnapshot


@dataclass(frozen=True, kw_only=True)
class TurnInputSource(ABC):
    keys: tuple[str, ...]
    accepted_id: str | None
    goal_permission: TurnGoalPermission
    bypasses_goal_permit: ClassVar[bool] = False
    origins: ClassVar[tuple[Message, ...]] = ()

    def valid_keys(self, text: str) -> bool:
        return len(self.keys) <= 1

    @abstractmethod
    def allows_context(
        self, goal: Goal | None, wait: GoalWait | None, registry: RegistrySnapshot
    ) -> bool: ...

    @abstractmethod
    def display(self, dispositions: InputDispositions, text: str) -> str | None: ...

    def selected_admission(self, inputs: InputDrain, session_id: str):
        return None

    def consume_wait(self, comms: Comms, canonical: str, wait: GoalWait | None) -> bool:
        return True

    def defers_for_goal(self, original: Goal | None, current: Goal | None) -> bool:
        return False


@dataclass(frozen=True, kw_only=True)
class OriginalTurnInput(TurnInputSource):
    prompt: str
    original_display: str | None
    origins: tuple[Message, ...]
    direct_origins: tuple[Message, ...]
    dependency_wait_id: str | None
    channel_batch: bool

    def valid_keys(self, text: str) -> bool:
        return super().valid_keys(text) or (self.channel_batch and text == self.prompt)

    def selected_admission(self, inputs: InputDrain, session_id: str):
        return inputs.selected_summary_admissions.get(session_id)

    def display(self, dispositions: InputDispositions, text: str) -> str | None:
        return self.original_display

    def consume_wait(self, comms: Comms, canonical: str, wait: GoalWait | None) -> bool:
        return (
            wait is None
            or self.dependency_wait_id is None
            or comms.goals.consume_goal_wait(canonical, wait.wait_id)
        )

    def allows_context(
        self, goal: Goal | None, wait: GoalWait | None, registry: RegistrySnapshot
    ) -> bool:
        return (
            self.goal_permission.allows(goal)
            and (
                self.dependency_wait_id is None
                or (wait is not None and wait.wait_id == self.dependency_wait_id)
            )
            and self.allows_wait(goal, wait, registry)
        )

    @abstractmethod
    def allows_wait(
        self, goal: Goal | None, wait: GoalWait | None, registry: RegistrySnapshot
    ) -> bool: ...


class OwnerOriginalInput(OriginalTurnInput):
    def allows_wait(
        self, goal: Goal | None, wait: GoalWait | None, registry: RegistrySnapshot
    ) -> bool:
        return True


class RoutedOriginalInput(OriginalTurnInput):
    def allows_wait(
        self, goal: Goal | None, wait: GoalWait | None, registry: RegistrySnapshot
    ) -> bool:
        return (
            wait is None
            or (
                wait.wait_id == self.dependency_wait_id
                and any(wait.matches(origin, registry) for origin in self.direct_origins)
            )
        ) and (
            not self.keys
            or goal is None
            or not goal.state.active
            or self.dependency_wait_id is not None
        )


class FollowingTurnInput(TurnInputSource):
    def display(self, dispositions: InputDispositions, text: str) -> str:
        row = dispositions.read().lookup(self.keys[0]) if self.keys else None
        return row.source_text if row is not None and row.exists else text

    def defers_for_goal(self, original: Goal | None, current: Goal | None) -> bool:
        return (
            (original is None or not original.state.active)
            and current is not None
            and current.state.active
        )


class AcceptedFollowingInput(FollowingTurnInput):
    bypasses_goal_permit = True

    def allows_context(
        self, goal: Goal | None, wait: GoalWait | None, registry: RegistrySnapshot
    ) -> bool:
        # QueuedInput.current additionally verifies the exact accepted goal/wait.
        return self.goal_permission.allows(goal)


class RoutedFollowingInput(FollowingTurnInput):
    def allows_context(
        self, goal: Goal | None, wait: GoalWait | None, registry: RegistrySnapshot
    ) -> bool:
        return (
            self.goal_permission.allows(goal)
            and wait is None
            and (not self.keys or goal is None or not goal.state.active)
        )
