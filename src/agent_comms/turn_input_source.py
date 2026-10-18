"""Original and follow-up inputs own their captured goal/wait and display semantics."""

from __future__ import annotations

from abc import ABC, abstractmethod
from contextlib import ExitStack
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, ClassVar

from acp import RequestError

from .agent_backend import WhenBusy
from .goal_attempts import GoalAttemptError, NoGoalPermit
from .channel_input_batch import InputBatch, SingleInputBatch
from .goals import (
    NoGoal,
    OwnerGoalPermission,
    TurnGoalPermission,
)
from .messages import Message
from .routing import ScheduledTurn
from .started_input import StartedInput

if TYPE_CHECKING:
    from .comms import Comms
    from .goal_waits import GoalWait
    from .goals import GoalDeclaration
    from .goals import Goal
    from .input_disposition import InputDispositions
    from .thread_identity import RecordedThreadIncarnation, TurnId
    from .threads import Thread


@dataclass(frozen=True, kw_only=True)
class TurnInputSource(StartedInput, ABC):
    accepted_id: str | None
    goal_permission: TurnGoalPermission
    origins: ClassVar[tuple[Message, ...]] = ()

    if TYPE_CHECKING:
        keys: tuple[str, ...]

    def valid_keys(self, text: str) -> bool:
        return len(self.keys) <= 1

    def dependency_current(self, wait: GoalWait) -> bool:
        return True

    @abstractmethod
    def allows_wait(self, wait: GoalWait, comms: Comms,
                    owner: RecordedThreadIncarnation) -> bool: ...

    @abstractmethod
    def allows_goal_input(self, goal: GoalDeclaration) -> bool: ...

    @abstractmethod
    def display(self, dispositions: InputDispositions, text: str) -> str | None: ...

    def consume_wait(self, comms: Comms, canonical: str, wait: GoalWait) -> bool:
        return True

    def defers_for_goal(self, original: GoalDeclaration, current: GoalDeclaration) -> bool:
        return False

    def require_goal_grant(self, permit, store) -> None:
        permit.require_send(store)


class InputDependency(ABC):
    @abstractmethod
    def current(self, wait: GoalWait) -> bool: ...

    @abstractmethod
    def allows(self, wait: GoalWait, comms: Comms, origins: tuple[Message, ...],
               owner: RecordedThreadIncarnation) -> bool: ...

    @abstractmethod
    def consume(self, comms: Comms, canonical: str, wait: GoalWait) -> bool: ...


class NoInputDependency(InputDependency):
    def current(self, wait: GoalWait) -> bool:
        return True

    def allows(self, wait: GoalWait, comms: Comms, origins: tuple[Message, ...],
               owner: RecordedThreadIncarnation) -> bool:
        return wait.allows_routed_input()

    def consume(self, comms: Comms, canonical: str, wait: GoalWait) -> bool:
        return True


@dataclass(frozen=True)
class CapturedInputDependency(InputDependency):
    wait_id: str

    def current(self, wait: GoalWait) -> bool:
        return wait.matches_id(self.wait_id)

    def allows(self, wait: GoalWait, comms: Comms, origins: tuple[Message, ...],
               owner: RecordedThreadIncarnation) -> bool:
        if wait.allows_routed_input():
            return True
        if not self.current(wait):
            return False
        with comms.bus.log.certified_read() as source:
            for origin in origins:
                original = source.delivery(origin.seq)
                if original.message.reference == origin.reference and wait.matches(original, owner):
                    return True
        return False

    def consume(self, comms: Comms, canonical: str, wait: GoalWait) -> bool:
        return wait.consume_dependency(comms, canonical)


@dataclass(frozen=True, kw_only=True)
class OriginalTurnInput(TurnInputSource):
    prompt: str
    original_display: str | None
    origins: tuple[Message, ...]
    dependency: InputDependency
    batch: InputBatch

    @classmethod
    def allows_start_wait(cls, wait) -> bool:
        return wait.allows_routed_input()

    @classmethod
    def require_start_goal(cls, turn) -> None:
        pass

    @classmethod
    def prepare_goal(cls, turn, permits) -> bool:
        return turn.thread.goal.active().prepare_turn(turn, permits)

    @classmethod
    def reserve_goal(cls, turn, goal):
        try:
            return turn.runner.comms.goals.reserve_turn(goal, owner=turn.thread)
        except GoalAttemptError as error:
            return cls.reservation_failed(error)

    @classmethod
    def reservation_failed(cls, error):
        raise RequestError.invalid_params({
            "reason": "goal_attempt_unavailable",
            "details": "The goal has no launchable attempt. Inspect its state and use "
                       "Retry for a failed attempt; no prompt was sent to Pi.",
        }) from error

    @classmethod
    def prepare_without_goal(cls):
        return True

    @classmethod
    def original_display(cls, initial, task):
        return initial or task

    @classmethod
    def permission(cls, turn):
        return turn.thread.goal.active()

    @property
    def keys(self) -> tuple[str, ...]:
        return self.batch.keys

    def valid_keys(self, text: str) -> bool:
        return super().valid_keys(text) or (self.batch.allows_multiple and text == self.prompt)

    def reserve(
        self, dispositions: InputDispositions, owner: Thread, turn: TurnId, claim_scope: int,
        *, ownership: ExitStack,
    ) -> OriginalTurnInput:
        """Every original uses the same durable input authority before native preparation.

        ACP and bus inputs already own ingress keys. A scheduled original has no
        external ingress; its authorized turn supplies identity, never replay authority.
        """
        if self.keys:
            return self
        batch = dispositions.reserve_turn(
            owner.name, turn, claim_scope, self.prompt, ownership=ownership,
        )
        return replace(self, batch=batch)

    def display(self, dispositions: InputDispositions, text: str) -> str | None:
        return self.original_display

    def consume_wait(self, comms: Comms, canonical: str, wait: GoalWait) -> bool:
        return self.dependency.consume(comms, canonical, wait)

    def dependency_current(self, wait: GoalWait) -> bool:
        return self.dependency.current(wait)


class DirectInput(TurnInputSource):
    def allows_wait(self, wait: GoalWait, comms: Comms,
                    owner: RecordedThreadIncarnation) -> bool:
        return True

    def allows_goal_input(self, goal: GoalDeclaration) -> bool:
        return True


class OwnerOriginalInput(OriginalTurnInput, DirectInput):
    @classmethod
    def allows_start_wait(cls, wait) -> bool:
        return True

    @classmethod
    def require_start_goal(cls, turn) -> None:
        if not turn.original_goal_permission.allows(turn.thread.goal):
            raise RequestError.invalid_params({"reason": "input_authority_changed"})

    @classmethod
    def prepare_goal(cls, turn, permits) -> bool:
        return True

    @classmethod
    def permission(cls, turn):
        return OwnerGoalPermission(turn.thread.goal)

    @property
    def notice_keys(self) -> tuple[str, ...]:
        return self.keys

    @property
    def notice_text(self) -> str:
        return self.original_display or self.prompt


class RoutedInput(TurnInputSource):
    def allows_wait(self, wait: GoalWait, comms: Comms,
                    owner: RecordedThreadIncarnation) -> bool:
        return wait.allows_routed_input()

    def allows_goal_input(self, goal: GoalDeclaration) -> bool:
        return not self.keys or goal.allows_goalless()


class RoutedOriginalInput(OriginalTurnInput, RoutedInput):
    pass


class SelectedOriginalInput(RoutedOriginalInput):
    """The messages a selected turn answers: their assignments record them, not input rows."""

    @classmethod
    def answering(cls, prompt: str) -> SelectedOriginalInput:
        return cls(
            accepted_id=None,
            goal_permission=NoGoal(),
            prompt=prompt,
            original_display=None,
            origins=(),
            dependency=NoInputDependency(),
            batch=SingleInputBatch(()),
        )


class DependencyOriginalInput(OriginalTurnInput, DirectInput):
    def allows_wait(self, wait: GoalWait, comms: Comms,
                    owner: RecordedThreadIncarnation) -> bool:
        return self.dependency.allows(wait, comms, self.origins, owner)


class ScheduledOriginalInput(DependencyOriginalInput):
    """An internal scheduled original inherits reservation and dependency checks.

    Captured goal permission and an applicable launch permit authorize this input;
    having a durable input key cannot turn it into an unsolicited routed message.
    """


class AutonomousOriginalInput(DependencyOriginalInput):
    @classmethod
    def reservation_failed(cls, error):
        return NoGoalPermit()

    @classmethod
    def prepare_without_goal(cls):
        return False

    @classmethod
    def original_display(cls, initial, task):
        return None


class DependencyScheduledTurn(ScheduledTurn):
    input_type = DependencyOriginalInput


class AutonomousScheduledTurn(ScheduledTurn):
    def joins(self, head: ScheduledTurn) -> bool:
        return False

    input_type = AutonomousOriginalInput

    def batch(self, pending):
        return pending[:1], pending[1:]


@dataclass(frozen=True, kw_only=True)
class FollowingTurnInput(TurnInputSource):
    keys: tuple[str, ...]
    when_busy: ClassVar[WhenBusy] = WhenBusy.STEER

    def continue_response(self, response, parts):
        """An actual following request may revise the turn's automatic answer."""
        return response.later_input(parts)

    def display(self, dispositions: InputDispositions, text: str) -> str:
        row = dispositions.lookup(self.keys[0]) if self.keys else None
        return row.source_text if row is not None and row.exists else text

    def defers_for_goal(self, original: GoalDeclaration, current: GoalDeclaration) -> bool:
        return current.active().defers_following_from(original.active())


class AcceptedFollowingInput(FollowingTurnInput, DirectInput):
    def require_goal_grant(self, permit, store) -> None:
        pass


class RoutedFollowingInput(FollowingTurnInput, RoutedInput):
    pass


@dataclass(frozen=True, kw_only=True)
class DeliveredFollowingInput(AcceptedFollowingInput):
    """A pending bus message handed to the recipient's running turn."""

    origins: tuple[Message, ...]

    def continue_response(self, response, parts):
        """Background handling has no automatic reply of its own."""
        return response.notification(parts)
