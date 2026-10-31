"""Original and follow-up inputs own their captured goal/wait and display semantics."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, ClassVar

from agent_comms.turn_goal_permission import TurnGoalPermission

from . import input_attempt
from .acp_extension import InputStartedUpdate, NoQueueScope, UnidentifiedInputStarted
from .turn_inbox import BusyInputPolicy, SteerInputPolicy
from .channel_input_batch import InputBatch, SingleInputBatch
from .errors import RelationViolationError
from .nominal.field_codec import AcpSessionId, InputId, TurnId
from .input_attempt import InputAlreadyCleared, MissingInput
from .input_key import InputKey
from .messages import Message
from .ordinary_turn_rules import UnboundOrdinaryInputRule, UnconsumedDependencyRule
from .reservation_rules import ReservationViolationError
from .routing import NoTurnRouting, TurnRouting
from .send_outcome import DeferredSend, GrantedSend, RefusedSend, SendOutcome
from .started_input import StartedInput
from .transcript_routes import DisplayText, NoDisplayText, SentInputText

from .reservation_rules import ReservationRule, RuleCheck
from .registry_document import RegistrySnapshot
from .threads import Thread
if TYPE_CHECKING:
    from .acp_extension import QueueScope
    from .comms import Comms
    from .controller import Controller
    from .goal_waits import GoalWait
    from .goals import GoalDeclaration
    from .input_disposition import InputDispositions
    from .input_drain import DrainedSession, InputDrain
    from .owned_send import OwnedSend
    from .runtime_server import RuntimeServer
    from .thread_identity import RecordedThreadIncarnation


@dataclass(frozen=True, kw_only=True)
class OrdinaryOwnerCheck(RuleCheck):
    expected: Thread
    current: Thread
    registry: RegistrySnapshot
    canonical: str
    claim_generation: int
    turn: TurnId

    def worktree_matches(self) -> bool:
        return self.current.worktree == self.expected.worktree


class RunningOrdinaryOwnerCheck(OrdinaryOwnerCheck):
    """The running turn retains its project; a selection is for the next turn."""

    def worktree_matches(self) -> bool:
        return self.current.continues_running_project(self.expected)


class OrdinaryIncarnationRule(ReservationRule):
    check_type = OrdinaryOwnerCheck
    explanation = "The captured ordinary thread incarnation changed."

    def violated(self, check: OrdinaryOwnerCheck) -> bool:
        return not check.expected.incarnation.current(check.registry)


class OrdinaryRunningRule(ReservationRule):
    check_type = OrdinaryOwnerCheck
    explanation = "The ordinary owner is no longer running."

    def violated(self, check: OrdinaryOwnerCheck) -> bool:
        return not check.registry.status(check.canonical).running


class OrdinaryClaimRule(ReservationRule):
    check_type = OrdinaryOwnerCheck
    explanation = "The ordinary owner claim_scope generation changed."

    def violated(self, check: OrdinaryOwnerCheck) -> bool:
        return check.registry.claim_generation(check.canonical) != check.claim_generation


class OrdinaryProcessRule(ReservationRule):
    check_type = OrdinaryOwnerCheck
    explanation = "The ordinary turn belongs to another process incarnation."

    def violated(self, check: OrdinaryOwnerCheck) -> bool:
        return check.current.process_identity != check.expected.process_identity


class OrdinaryWorktreeRule(ReservationRule):
    check_type = OrdinaryOwnerCheck
    explanation = "The ordinary owner's worktree changed."

    def violated(self, check: OrdinaryOwnerCheck) -> bool:
        return not check.worktree_matches()


class OrdinaryTurnRule(ReservationRule):
    check_type = OrdinaryOwnerCheck
    explanation = "The captured ordinary turn is no longer the active turn."

    def violated(self, check: OrdinaryOwnerCheck) -> bool:
        return (
            not check.current.turn_record.read().matches(check.turn)
        )


class TurnAcceptance(ABC):
    """The live ACP acknowledgement, when this turn has one."""

    def controller(self, state: DrainedSession, runtime: RuntimeServer) -> Controller | None:
        return runtime.controller.get()

    @abstractmethod
    def require_current(
        self, inputs: InputDrain, session_id: AcpSessionId, source: TurnInputSource,
        current: Thread, claim_generation: int,
    ) -> None: ...

    async def started_update(
        self, source: TurnInputSource, inputs: InputDrain, session_id: AcpSessionId,
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
        self, update: InputStartedUpdate, inputs: InputDrain, session_id: AcpSessionId,
    ) -> InputStartedUpdate:
        return update

    def require_following_source(
        self, inputs: InputDrain, session_id: AcpSessionId, source: TurnInputSource,
    ) -> None:
        raise RelationViolationError("An input without an acceptance cannot own following context")

    @abstractmethod
    def release(self, inputs: InputDrain, session_id: AcpSessionId) -> None: ...


class NoTurnAcceptance(TurnAcceptance):
    """A scheduled or selected turn has no queued ACP acceptance."""

    def require_current(
        self, inputs: InputDrain, session_id: AcpSessionId, source: TurnInputSource,
        current: Thread, claim_generation: int,
    ) -> None:
        return

    def release(self, inputs: InputDrain, session_id: AcpSessionId) -> None:
        return


@dataclass(frozen=True, kw_only=True)
class TurnInputSource(StartedInput, ABC):
    acceptance: TurnAcceptance
    goal_permission: TurnGoalPermission
    bypasses_goal_permit: ClassVar[bool] = False
    owner_check: ClassVar[type[OrdinaryOwnerCheck]] = OrdinaryOwnerCheck
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
            sender._check_owner(
                current, snapshot, canonical, self, sent_text, check_type=self.owner_check,
            )
            # The queue grants a send, not a running tool's lifetime. Once Pi
            # starts this exact input, StartedInput removes its queue entry.
            self.acceptance.require_current(
                sender.inputs, sender.session_id, self, current, sender.claim_generation,
            )
        except ReservationViolationError as error:
            return sender._refusal(error.rule)
        if self.defers_send(sender, current):
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
            sent_text=SentInputText(sent_text),
            routing=TurnRouting(
                tuple(origin.reference for origin in self.origins)
            ) if self.origins else NoTurnRouting(),
        )
        return GrantedSend()

    def require_context_source(self, sender: OwnedSend) -> None:
        """An original's lease and durable binding fence its retained context."""

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
    def display(self, dispositions: InputDispositions, text: str) -> DisplayText | NoDisplayText: ...

    def consume_wait(self, comms: Comms, canonical: str, wait: GoalWait) -> bool:
        return True

    def defers_send(self, sender: OwnedSend, current: Thread) -> bool:
        return False


class InputDependency(ABC):
    def authorizes(self, wait: GoalWait, owner_input: bool) -> bool:
        return self.current(wait)

    def original_type(self) -> type[OriginalTurnInput]:
        return RoutedOriginalInput

    @abstractmethod
    def current(self, wait: GoalWait) -> bool: ...

    @abstractmethod
    def allows(self, wait: GoalWait, comms: Comms, origins: tuple[Message, ...],
               owner: RecordedThreadIncarnation) -> bool: ...

    @abstractmethod
    def consume(self, comms: Comms, canonical: str, wait: GoalWait) -> bool: ...


class NoInputDependency(InputDependency):
    def authorizes(self, wait: GoalWait, owner_input: bool) -> bool:
        return owner_input or wait.allows_routed_input()

    def current(self, wait: GoalWait) -> bool:
        return True

    def allows(self, wait: GoalWait, comms: Comms, origins: tuple[Message, ...],
               owner: RecordedThreadIncarnation) -> bool:
        return wait.allows_routed_input()

    def consume(self, comms: Comms, canonical: str, wait: GoalWait) -> bool:
        return True


@dataclass(frozen=True, kw_only=True)
class OriginalTurnInput(TurnInputSource):
    prompt: str
    original_display: DisplayText | NoDisplayText
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

    def display(self, dispositions: InputDispositions, text: str) -> DisplayText | NoDisplayText:
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
        return self.original_display.prompt_or(self.prompt)


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
            original_display=NoDisplayText(),
            origins=(),
            dependency=NoInputDependency(),
            batch=SingleInputBatch(()),
        )


class ScheduledOriginalInput(OriginalTurnInput, DirectInput):
    """Captured goal permission and a launch permit authorize an internal input.

    Having a durable input key cannot turn it into an unsolicited routed message.
    """

    def allows_wait(self, wait: GoalWait, comms: Comms,
                    owner: RecordedThreadIncarnation) -> bool:
        return self.dependency.allows(wait, comms, self.origins, owner)


@dataclass(frozen=True, kw_only=True)
class FollowingTurnInput(TurnInputSource):
    keys: tuple[InputKey, ...]
    when_busy: ClassVar[type[BusyInputPolicy]] = SteerInputPolicy
    owner_check = RunningOrdinaryOwnerCheck

    def require_context_source(self, sender: OwnedSend) -> None:
        self.acceptance.require_following_source(sender.inputs, sender.session_id, self)

    def continue_response(self, response, parts):
        """An actual following request may revise the turn's automatic answer."""
        return response.later_input(parts)

    def display(self, dispositions: InputDispositions, text: str) -> DisplayText:
        row = next((dispositions.lookup(key) for key in self.keys), MissingInput())
        return DisplayText(row.source_text_or(text))

    def defers_send(self, sender: OwnedSend, current: Thread) -> bool:
        """Keep unwritten input for a fresh turn in the selected project and goal."""
        return (
            sender.thread.worktree != current.worktree
            or not sender.goal_permission.allows(current.goal)
        )


class AcceptedFollowingInput(FollowingTurnInput, DirectInput):
    bypasses_goal_permit = True


@dataclass(frozen=True, kw_only=True)
class DeliveredFollowingInput(AcceptedFollowingInput):
    """A pending bus message handed to the recipient's running turn."""

    origins: tuple[Message, ...]

    def continue_response(self, response, parts):
        """Background handling has no automatic reply of its own."""
        return response.notification(parts)
