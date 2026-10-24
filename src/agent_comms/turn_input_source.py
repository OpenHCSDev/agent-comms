"""Original and follow-up inputs own their captured goal/wait and display semantics."""

from __future__ import annotations

from abc import ABC, abstractmethod
from contextlib import ExitStack
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, ClassVar

from agent_comms.turn_goal_permission import GoalPermission, TurnGoalPermission

from . import input_attempt
from .acp_extension import InputStartedUpdate, NoQueueScope
from .agent_backend import BusyInputPolicy, SteerInputPolicy
from .channel_input_batch import InputBatch, SingleInputBatch
from .field_codec import InputId
from .messages import Message
from .ordinary_turn_rules import AcceptedInputCheck, MissingAcceptedInputCheck
from .started_input import StartedInput

if TYPE_CHECKING:
    from .acp_extension import QueueScope
    from .comms import Comms
    from .controller import Controller
    from .goal_waits import GoalWait
    from .goals import GoalDeclaration
    from .input_disposition import InputDispositions
    from .input_drain import DrainedSession, InputDrain
    from .runtime import RuntimeServer
    from .thread_identity import RecordedThreadIncarnation, RegisteredTurn
    from .threads import Thread


class TurnAcceptance(ABC):
    """The live ACP acknowledgement, when this turn has one."""

    def controller(self, state: DrainedSession, runtime: RuntimeServer) -> Controller | None:
        return runtime.controller.get()

    @abstractmethod
    def require_current(
        self, inputs: InputDrain, session_id: str, source: TurnInputSource,
        current: Thread, claim_generation: int,
    ) -> None: ...

    async def started_update(
        self, source: TurnInputSource, inputs: InputDrain, session_id: str,
        row: input_attempt.StartedInput, scope: QueueScope,
    ) -> InputStartedUpdate:
        current = await inputs.queue_binding(session_id)
        if not scope.current_for(current):
            current = NoQueueScope()
        return InputStartedUpdate(
            row.public_id if source.notice_keys else None, source.notice_text,
            current, current.start_revision(inputs, session_id), row.native_id,
        )

    @abstractmethod
    def release(self, inputs: InputDrain, session_id: str) -> None: ...


class NoTurnAcceptance(TurnAcceptance):
    """A scheduled or selected turn has no queued ACP acceptance."""

    def require_current(
        self, inputs: InputDrain, session_id: str, source: TurnInputSource,
        current: Thread, claim_generation: int,
    ) -> None:
        return

    def release(self, inputs: InputDrain, session_id: str) -> None:
        return


@dataclass(frozen=True)
class AcceptedTurn(TurnAcceptance):
    input_id: InputId

    def controller(self, state: DrainedSession, runtime: RuntimeServer) -> Controller | None:
        return state.queued[self.input_id].controller

    def require_current(
        self, inputs: InputDrain, session_id: str, source: TurnInputSource,
        current: Thread, claim_generation: int,
    ) -> None:
        try:
            accepted = inputs.state(session_id).queued[self.input_id]
        except KeyError:
            for row in inputs.dispositions.read(source.keys).rows.values():
                row.require_not_cleared()
            MissingAcceptedInputCheck().require_valid()
            raise
        AcceptedInputCheck(
            acknowledgement=accepted, current=current, claim_generation=claim_generation,
            input_id=self.input_id, keys=source.keys, inputs=inputs, session_id=session_id,
        ).require_valid()

    async def started_update(
        self, source: TurnInputSource, inputs: InputDrain, session_id: str,
        row: input_attempt.StartedInput, scope: QueueScope,
    ) -> InputStartedUpdate:
        text = source.notice_text
        try:
            item = inputs.state(session_id).queued[self.input_id]
        except KeyError:
            pass
        else:
            text = await item.started(inputs)
        update = await super().started_update(source, inputs, session_id, row, scope)
        return replace(
            update, input_id=row.public_id if source.notice_keys else self.input_id, text=text,
        )

    def release(self, inputs: InputDrain, session_id: str) -> None:
        inputs.state(session_id).queued.pop(self.input_id, None)


@dataclass(frozen=True, kw_only=True)
class TurnInputSource(StartedInput, ABC):
    acceptance: TurnAcceptance
    goal_permission: TurnGoalPermission
    bypasses_goal_permit: ClassVar[bool] = False
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

    def defers_for_goal(self, original: GoalPermission, current: GoalDeclaration) -> bool:
        return False


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
        with comms.bus.bus.reading() as source:
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

    @property
    def keys(self) -> tuple[str, ...]:
        return self.batch.keys

    def valid_keys(self, text: str) -> bool:
        return super().valid_keys(text) or (self.batch.allows_multiple and text == self.prompt)

    def reserve(
        self, dispositions: InputDispositions, owner: Thread, turn: RegisteredTurn,
        claim_scope: int,
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
        return self.goal_permission.dependency_current(self.dependency, wait)


class DirectInput(TurnInputSource):
    def allows_wait(self, wait: GoalWait, comms: Comms,
                    owner: RecordedThreadIncarnation) -> bool:
        return True

    def allows_goal_input(self, goal: GoalDeclaration) -> bool:
        return True


class OwnerOriginalInput(OriginalTurnInput, DirectInput):
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
        return not self.keys or self.goal_permission.allows_routed(goal)


class RoutedOriginalInput(OriginalTurnInput, RoutedInput):
    pass


class SelectedOriginalInput(RoutedOriginalInput):
    """The messages a selected turn answers: their assignments record them, not input rows."""

    @classmethod
    def answering(cls, prompt: str, permission: TurnGoalPermission) -> SelectedOriginalInput:
        return cls(
            acceptance=NoTurnAcceptance(),
            goal_permission=permission,
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


@dataclass(frozen=True, kw_only=True)
class FollowingTurnInput(TurnInputSource):
    keys: tuple[str, ...]
    when_busy: ClassVar[type[BusyInputPolicy]] = SteerInputPolicy

    def continue_response(self, response, parts):
        """An actual following request may revise the turn's automatic answer."""
        return response.later_input(parts)

    def display(self, dispositions: InputDispositions, text: str) -> str:
        row = dispositions.lookup(self.keys[0]) if self.keys else None
        return row.source_text if row is not None and row.exists else text

    def defers_for_goal(self, original: GoalPermission, current: GoalDeclaration) -> bool:
        return not original.allows(current)


class AcceptedFollowingInput(FollowingTurnInput, DirectInput):
    bypasses_goal_permit = True


@dataclass(frozen=True, kw_only=True)
class DeliveredFollowingInput(AcceptedFollowingInput):
    """A pending bus message handed to the recipient's running turn."""

    origins: tuple[Message, ...]

    def continue_response(self, response, parts):
        """Background handling has no automatic reply of its own."""
        return response.notification(parts)
