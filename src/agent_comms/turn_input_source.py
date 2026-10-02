"""Original and follow-up inputs own their captured goal/wait and display semantics."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, ClassVar

from .channel_input_batch import InputBatch, SingleInputBatch
from .messages import Message
from .turn_goal_permission import InactiveGoalPermission, TurnGoalPermission

if TYPE_CHECKING:
    from .comms import Comms
    from .goal_waits import GoalWait
    from .goals import Goal
    from .input_disposition import InputDispositions
    from .input_drain import InputDrain
    from .thread_identity import TurnId
    from .threads import Thread


@dataclass(frozen=True, kw_only=True)
class TurnInputSource(ABC):
    accepted_id: str | None
    goal_permission: TurnGoalPermission
    bypasses_goal_permit: ClassVar[bool] = False
    origins: ClassVar[tuple[Message, ...]] = ()

    if TYPE_CHECKING:
        keys: tuple[str, ...]

    def valid_keys(self, text: str) -> bool:
        return len(self.keys) <= 1

    def dependency_current(self, wait: GoalWait | None) -> bool:
        return True

    @abstractmethod
    def allows_wait(self, wait: GoalWait | None, comms: Comms) -> bool: ...

    @abstractmethod
    def allows_goal_input(self, goal: Goal | None) -> bool: ...

    @abstractmethod
    def display(self, dispositions: InputDispositions, text: str) -> str | None: ...

    def selected_admission(self, inputs: InputDrain, session_id: str):
        return None

    def consume_wait(self, comms: Comms, canonical: str, wait: GoalWait | None) -> bool:
        return True

    def defers_for_goal(self, original: Goal | None, current: Goal | None) -> bool:
        return False


class InputDependency(ABC):
    @abstractmethod
    def current(self, wait: GoalWait | None) -> bool: ...

    @abstractmethod
    def allows(self, wait: GoalWait | None, comms: Comms, origins: tuple[Message, ...]) -> bool: ...

    @abstractmethod
    def consume(self, comms: Comms, canonical: str, wait: GoalWait | None) -> bool: ...


class NoInputDependency(InputDependency):
    def current(self, wait: GoalWait | None) -> bool:
        return True

    def allows(self, wait: GoalWait | None, comms: Comms, origins: tuple[Message, ...]) -> bool:
        return wait is None

    def consume(self, comms: Comms, canonical: str, wait: GoalWait | None) -> bool:
        return True


@dataclass(frozen=True)
class CapturedInputDependency(InputDependency):
    wait_id: str

    def current(self, wait: GoalWait | None) -> bool:
        return wait is not None and wait.wait_id == self.wait_id

    def allows(self, wait: GoalWait | None, comms: Comms, origins: tuple[Message, ...]) -> bool:
        if wait is None:
            return True
        if not self.current(wait):
            return False
        with comms.bus.log.certified_read() as source:
            for origin in origins:
                original = source.delivery(origin.seq)
                if original.message.reference == origin.reference and wait.matches(original):
                    return True
        return False

    def consume(self, comms: Comms, canonical: str, wait: GoalWait | None) -> bool:
        return wait is None or comms.goals.consume_goal_wait(canonical, wait.wait_id)


@dataclass(frozen=True, kw_only=True)
class OriginalTurnInput(TurnInputSource):
    prompt: str
    original_display: str | None
    origins: tuple[Message, ...]
    dependency: InputDependency
    batch: InputBatch

    @property
    def keys(self) -> tuple[str, ...]:
        return self.batch.keys

    @property
    def notice_keys(self) -> tuple[str, ...]:
        return ()

    @property
    def notice_text(self) -> str | None:
        return None

    def valid_keys(self, text: str) -> bool:
        return super().valid_keys(text) or (self.batch.admits_multiple and text == self.prompt)

    def compaction_keys(self, session_file: str | None) -> tuple[str, ...]:
        """Every original in this captured input shares its saved-context preparation."""
        return self.keys if session_file is not None else ()

    def reserve(
        self, dispositions: InputDispositions, owner: Thread, turn: TurnId, admission: int
    ) -> OriginalTurnInput:
        """Every original uses the same durable input authority before native preparation.

        ACP and bus inputs already own ingress keys. A scheduled original has no
        external ingress; its admitted turn supplies identity, never replay authority.
        """
        if self.keys:
            return self
        key = dispositions.reserve_turn(owner.name, turn, admission, self.prompt)
        return replace(self, batch=SingleInputBatch(dispositions.read().originals((key,))))

    def selected_admission(self, inputs: InputDrain, session_id: str):
        return inputs.selected_summary_admissions.get(session_id)

    def display(self, dispositions: InputDispositions, text: str) -> str | None:
        return self.original_display

    def consume_wait(self, comms: Comms, canonical: str, wait: GoalWait | None) -> bool:
        return self.dependency.consume(comms, canonical, wait)

    def dependency_current(self, wait: GoalWait | None) -> bool:
        return self.dependency.current(wait)


class DirectInput(TurnInputSource):
    def allows_wait(self, wait: GoalWait | None, comms: Comms) -> bool:
        return True

    def allows_goal_input(self, goal: Goal | None) -> bool:
        return True


class OwnerOriginalInput(OriginalTurnInput, DirectInput):
    @property
    def notice_keys(self) -> tuple[str, ...]:
        return self.keys

    @property
    def notice_text(self) -> str:
        return self.original_display or self.prompt


class RoutedInput(TurnInputSource):
    def allows_wait(self, wait: GoalWait | None, comms: Comms) -> bool:
        return wait is None

    def allows_goal_input(self, goal: Goal | None) -> bool:
        return not self.keys or InactiveGoalPermission().allows(goal)


class RoutedOriginalInput(OriginalTurnInput, RoutedInput):
    pass


class DependencyOriginalInput(OriginalTurnInput, DirectInput):
    def allows_wait(self, wait: GoalWait | None, comms: Comms) -> bool:
        return self.dependency.allows(wait, comms, self.origins)


class ScheduledOriginalInput(DependencyOriginalInput):
    """An internal scheduled original inherits reservation and dependency checks.

    Captured goal permission and an applicable launch permit authorize this input;
    having a durable input key cannot turn it into an unsolicited routed message.
    """


@dataclass(frozen=True, kw_only=True)
class FollowingTurnInput(TurnInputSource):
    keys: tuple[str, ...]

    def display(self, dispositions: InputDispositions, text: str) -> str:
        row = dispositions.read().lookup(self.keys[0]) if self.keys else None
        return row.source_text if row is not None and row.exists else text

    def defers_for_goal(self, original: Goal | None, current: Goal | None) -> bool:
        return (
            (original is None or not original.state.active)
            and current is not None
            and current.state.active
        )


class AcceptedFollowingInput(FollowingTurnInput, DirectInput):
    bypasses_goal_permit = True


class RoutedFollowingInput(FollowingTurnInput, RoutedInput):
    pass
