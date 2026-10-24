"""Original and follow-up inputs own their captured goal/wait and display semantics."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, ClassVar

from agent_comms.turn_goal_permission import GoalPermission, TurnGoalPermission

from . import input_attempt
from .acp_extension import InputStartedUpdate, NoQueueScope, UnidentifiedInputStarted
from .agent_backend import BusyInputPolicy, SteerInputPolicy
from .channel_input_batch import InputBatch, SingleInputBatch
from .field_codec import GoalWaitId, InputId, TurnId
from .input_attempt import InputAlreadyCleared
from .input_key import InputKey
from .messages import Message
from .ordinary_turn_rules import (
    AcceptedInputCheck,
    MissingAcceptedInputCheck,
    UnboundOrdinaryInputRule,
    UnconsumedDependencyRule,
)
from .reservation_rules import ReservationViolationError
from .routing import TurnRouting
from .send_outcome import DeferredSend, GrantedSend, RefusedSend, SendOutcome
from .started_input import StartedInput

if TYPE_CHECKING:
    from .acp_extension import QueueScope
    from .comms import Comms
    from .controller import Controller
    from .goal_waits import GoalWait
    from .goals import GoalDeclaration
    from .input_disposition import InputDispositions
    from .input_drain import DrainedSession, InputDrain
    from .owned_send import OwnedSend
    from .runtime import RuntimeServer
    from .thread_identity import RecordedThreadIncarnation
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
        update = UnidentifiedInputStarted(
            source.notice_text, current, current.start_revision(inputs, session_id), row.native_id,
        )
        accepted = await self.accepted_start(update, inputs, session_id)
        return source.started_update_of(row, accepted)

    async def accepted_start(
        self, update: InputStartedUpdate, inputs: InputDrain, session_id: str,
    ) -> InputStartedUpdate:
        return update

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

    async def accepted_start(
        self, update: InputStartedUpdate, inputs: InputDrain, session_id: str,
    ) -> InputStartedUpdate:
        queued = inputs.state(session_id).queued
        if self.input_id in queued:
            update = replace(update, text=await queued[self.input_id].started(inputs))
        return update.identified(self.input_id)

    def release(self, inputs: InputDrain, session_id: str) -> None:
        inputs.state(session_id).queued.pop(self.input_id, None)


@dataclass(frozen=True, kw_only=True)
class TurnInputSource(StartedInput, ABC):
    acceptance: TurnAcceptance
    goal_permission: TurnGoalPermission
    bypasses_goal_permit: ClassVar[bool] = False
    origins: ClassVar[tuple[Message, ...]] = ()

    if TYPE_CHECKING:
        keys: tuple[InputKey, ...]

    @contextmanager
    def prepare_send(
        self, sender: OwnedSend, native_id: InputId, sent_text: str,
        *, already_bound: bool = False,
    ) -> Iterator[SendOutcome]:
        try:
            decision = self._prepare(sender, native_id, sent_text, already_bound=already_bound)
        except InputAlreadyCleared:
            decision = RefusedSend()
        yield decision

    def _prepare(
        self, sender: OwnedSend, native_id: InputId, sent_text: str,
        *, already_bound: bool = False,
    ) -> SendOutcome:
        # Backend input handling holds shared locks through this check and write.
        snapshot = sender.comms.registry.snapshot()
        canonical = snapshot.canonical_name(sender.thread.name)
        try:
            current = sender.require_owner(snapshot, canonical)
            sender._check_owner(current, snapshot, canonical, self, sent_text)
            # The queue grants a send, not a running tool's lifetime. Once Pi
            # starts this exact input, StartedInput removes its queue entry.
            self.acceptance.require_current(
                sender.inputs, sender.session_id, self, current, sender.claim_generation,
            )
        except ReservationViolationError as error:
            return sender._refusal(error.rule)
        if self.defers_for_goal(sender.goal_permission, current.goal):
            return DeferredSend()
        wait = sender.comms.goals.goal_wait(canonical)
        try:
            sender._require_context(self, current, wait)
        except ReservationViolationError as error:
            return sender._refusal(error.rule)
        # Binding failures may be uncertain. Exceptions here still propagate;
        # never reinterpret a failed durable operation as a retryable policy refusal.
        if not sender.inputs.dispositions.bind_turn_input(
            self.keys,
            claim_scope=sender.claim_generation,
            turn_id=sender.turn,
            native_id=native_id,
            text=sent_text,
            already_bound=already_bound,
        ):
            return sender._refusal(UnboundOrdinaryInputRule())
        if not self.consume_wait(sender.comms, canonical, wait):
            return sender._refusal(UnconsumedDependencyRule())
        sender.comms.transcripts.routes.record_input_display(
            native_id,
            self.display(sender.inputs.dispositions, sent_text),
            sent_text=sent_text,
            routing=TurnRouting(
                tuple(origin.reference for origin in self.origins), None
            ) if self.origins else None,
        )
        return GrantedSend()

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
    wait_id: GoalWaitId

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
    def keys(self) -> tuple[InputKey, ...]:
        return self.batch.keys

    def valid_keys(self, text: str) -> bool:
        return super().valid_keys(text) or (self.batch.allows_multiple and text == self.prompt)

    def reserve(
        self, dispositions: InputDispositions, owner: Thread, turn: TurnId,
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
    def started_update_of(
        self, row: input_attempt.StartedInput, update: InputStartedUpdate,
    ) -> InputStartedUpdate:
        return row.key.identify(update)

    def failure_state(self, document):
        return document.shared_state(self.keys)

    def failed_input_text(self, document) -> str | None:
        return self.notice_text if not document.all_started(self.keys) else None

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
    keys: tuple[InputKey, ...]
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
