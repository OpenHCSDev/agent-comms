"""Tracked native turns share Pi declarations and the existing turn-session authority."""

from __future__ import annotations

import asyncio
import time
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable, Mapping
from contextlib import AsyncExitStack, ExitStack
from dataclasses import dataclass, field
from functools import partial
from pathlib import Path
from types import MappingProxyType
from typing import TYPE_CHECKING, Generic, TypeVar

from metaclass_registry.mro_dispatch import handles

from . import pi_commands as commands
from . import pi_events as pi
from .agent_events import AgentEvent
from .backend import TurnSession
from .coordinator import Coordination
from .errors import RelationViolationError
from .external_transcript import DraftRow
from .field_codec import InputId, MalformedRecord
from .following_inputs import FollowingInputs
from .maintenance_barrier import UNCOORDINATED_INGRESS, MaintenanceIngress
from .native_arguments import SelectedModel
from .native_attestation import AttestationError, ObservedAttestation
from .native_failures import NativePiUnavailable
from .native_package import VerifiedNativePackage
from .native_input_proof import NativeContextProof
from .native_pi import (
    NativeEmptyAnswer,
    NativePiPromptRejected,
    NativeTurnResult,
    _verify_context,
    read_tracked_input_commitment,
)
from .private_sidecar import native_request_digest
from .native_prompt_send import UNGUARDED_PROMPT, PromptBoundary, PromptSendFailure
from .native_session_reopen import SessionRetention
from .native_startup import NativeStartup
from .native_tool_call import NativeToolMode
from .pi_rpc import PiRpcChannel
from .pi_vocabulary import NoThinkingOverride, ThinkingChoice
from .request_progress import NO_REQUEST_OBSERVER, RequestObserver
from .selected_session import SelectedSession
from .tool_denial import SelectedToolDenied
from .turn_context import InputContributionCoordinates
from .turn_watchdog import DEFAULT_MODEL_WAIT, TurnWait, TurnWaitExpired

if TYPE_CHECKING:
    from .acp_failure import ProviderRetryResult
    from .agent_backend import TurnInbox
    from .coordinated_runtime import NativeExecution
    from .cursor_publication import CursorDelivery
    from .owned_send import OwnedSend
    from .native_entries import NativeEvidenceScope
    from .native_tool_transport import UnboundToolTransport
    from .native_session_files import NativeSessionFiles
    from .native_session_reopen import PiSessionId
    from .pi_native_backend import PersistentPiSession
    from .pi_payloads import UserMessage
    from .turn_failure import TurnFailure


NativeObserver = Callable[[pi.PiEvent | AgentEvent | ObservedAttestation], Awaitable[None]]


class NoObserver:
    async def __call__(self, event: pi.PiEvent | AgentEvent | ObservedAttestation) -> None:
        pass


NO_NATIVE_OBSERVER = NoObserver()
NO_FOLLOWING_INPUTS = FollowingInputs()

T = TypeVar("T")


class NativeCommitObservation(ABC, Generic[T]):
    """Observed native input records, without permission to start or replay input."""

    @abstractmethod
    def require(self) -> T: ...

    def capture(self, event: T) -> NativeCommitObservation[T]:
        raise NativePiUnavailable("Native Pi repeated the input commitment")


class PendingNativeCommit(NativeCommitObservation[T]):
    def require(self) -> T:
        raise NativePiUnavailable("Native Pi did not commit a tracked model context")

    def capture(self, event: T) -> NativeCommitObservation[T]:
        return ObservedNativeCommit(event)


@dataclass(frozen=True)
class ObservedNativeCommit(NativeCommitObservation[T]):
    event: T

    def require(self) -> T:
        return self.event


@dataclass
class TrackedInputContext:
    """One actually sent command owns its emitted join, bounded by this run's scope."""

    command: commands.Prompt
    input_commit: NativeCommitObservation[pi.InputCommitted] = field(default_factory=PendingNativeCommit)
    context_commit: NativeCommitObservation[pi.ContextCommitted] = field(default_factory=PendingNativeCommit)

    async def committed_input(self, turn: TrackedTurnSession, event: pi.InputCommitted) -> None:
        self.input_commit = self.input_commit.capture(event)
        async with turn.cursor_delivery.reading() as source_reads:
            await Coordination.run_worker(lambda:
                source_reads.for_source(turn.active_session_file).observe(event))
        await self.command.input_committed(turn, event)

    def proof(
        self, files: NativeSessionFiles, session_id: PiSessionId,
        source_reads: NativeEvidenceScope,
    ) -> NativeContextProof:
        evidence = source_reads.for_source(files.session)
        proof = _verify_context(
            files, self.command.input_id, session_id,
            self.input_commit.require(), self.context_commit.require(), evidence=evidence,
        )
        committed = read_tracked_input_commitment(
            proof.session_file, self.command.input_id, evidence=evidence,
        )
        if not committed.matches(native_request_digest(
            self.command.message, streaming_behavior=self.command.streaming_behavior,
            images=self.command.images,
        )):
            raise NativePiUnavailable("Native context differs from its actual scoped input request")
        return proof

    async def committed_context(self, turn: TrackedTurnSession, event: pi.ContextCommitted) -> None:
        self.context_commit = ObservedNativeCommit(event)
        async with turn.cursor_delivery.reading() as source_reads:
            proof = await Coordination.run_worker(partial(
                self.proof, turn.launch.session.files(turn.launch.root),
                turn.native.attestation.require_identity().session_id, source_reads,
            ))
            await self.command.context_committed(turn, proof, source_reads)


class TrackedTerminal(ABC):
    """Relate completed assistant responses to their streamed text."""

    def append(self, text: str) -> TrackedTerminal:
        return self.answered(text) if text else self.empty_answer()

    def answered(self, text: str) -> TrackedTerminal:
        return CompletedTrackedTerminal(text)

    def empty_answer(self) -> TrackedTerminal:
        return EmptyAnswerTrackedTerminal()

    def tool_round(self) -> TrackedTerminal:
        return PendingTrackedTerminal()

    def later_input(self, parts: list[str]) -> TrackedTerminal:
        return PendingTrackedTerminal()

    def notification(self, parts: list[str]) -> TrackedTerminal:
        return self.later_input(parts).keep_response()

    def keep_response(self) -> TrackedTerminal:
        return UnansweredNotifiedTrackedTerminal()

    @abstractmethod
    def require_response(self, parts: list[str]) -> str: ...


class EmptyAnswerTrackedTerminal(TrackedTerminal):
    """An observed empty final is a model outcome, not a missing native response."""

    def notification(self, parts: list[str]) -> TrackedTerminal:
        return self

    def require_response(self, parts: list[str]) -> str:
        raise NativeEmptyAnswer()


class PendingTrackedTerminal(TrackedTerminal):
    def notification(self, parts):
        # A following bus message cannot answer an already completed request,
        # but neither can it erase a final answer still owed after a tool round.
        return self

    def require_response(self, parts):
        raise NativePiUnavailable("Native Pi has no unique authoritative completed response")


@dataclass(frozen=True)
class CompletedTrackedTerminal(TrackedTerminal):
    text: str

    def answered(self, text: str) -> TrackedTerminal:
        return AmbiguousTrackedTerminal()

    def empty_answer(self) -> TrackedTerminal:
        return AmbiguousTrackedTerminal()

    def later_input(self, parts):
        return AnsweredTrackedTerminal(self.text) if self.matches(parts) else PendingTrackedTerminal()

    def matches(self, parts: list[str]) -> bool:
        return "".join(parts) == self.text

    def require_response(self, parts):
        if not self.matches(parts):
            raise NativePiUnavailable("Native Pi has no unique authoritative completed response")
        return self.text


@dataclass(frozen=True)
class AnsweredTrackedTerminal(TrackedTerminal):
    """The turn answered, then took a later input; that answer stands unless the model writes another."""

    text: str

    def empty_answer(self) -> TrackedTerminal:
        return self

    def tool_round(self):
        return self

    def later_input(self, parts):
        return self

    def require_response(self, parts):
        return self.text

    def keep_response(self):
        return NotifiedTrackedTerminal(self.text)


class NotificationTrackedTerminal(TrackedTerminal):
    """Notice handling cannot answer or revise the original requested task."""

    def answered(self, text: str) -> TrackedTerminal:
        return self

    def empty_answer(self) -> TrackedTerminal:
        return self

    def tool_round(self):
        return self

    def notification(self, parts):
        return self


class NotifiedTrackedTerminal(NotificationTrackedTerminal, AnsweredTrackedTerminal):
    def later_input(self, parts):
        return AnsweredTrackedTerminal(self.text)


class UnansweredNotifiedTrackedTerminal(NotificationTrackedTerminal, PendingTrackedTerminal):
    def empty_answer(self) -> TrackedTerminal:
        return EmptyAnswerTrackedTerminal()


class AmbiguousTrackedTerminal(PendingTrackedTerminal):
    def answered(self, text: str) -> TrackedTerminal:
        return self

    def empty_answer(self) -> TrackedTerminal:
        return self


class TrackedTurnSession(TurnSession):
    """Own one strict input/proof exchange; never grant publication or retry authority."""

    def __init__(
        self,
        launch,
        command,
        *,
        model: SelectedModel,
        model_wait: TurnWait,
        prompt_send_boundary,
        ingress: MaintenanceIngress,
        startup: NativeStartup,
        selected_tool_mode,
        observe_event: NativeObserver,
        request_observer: RequestObserver,
        pi_session: PersistentPiSession,
        cursor_delivery: CursorDelivery,
        following: FollowingInputs,
    ):
        # The owner's Pi session child: an idle child left by its previous
        # input is reused when its launch and source still match.
        super().__init__(
            launch,
            command,
            tool_mode=selected_tool_mode,
            model_wait=model_wait,
            startup=startup,
            request_observer=request_observer,
            native_session=pi_session,
        )
        self.retainable = False
        self.cursor_delivery = cursor_delivery
        self.following = following
        self.model = model
        self.prompt_send_boundary = prompt_send_boundary
        self.ingress = ingress
        self.observe_event = observe_event
        self.contexts: dict[InputId, TrackedInputContext] = {}
        self.draft_written_at = 0.0
        self.terminal: TrackedTerminal = PendingTrackedTerminal()

    async def open_transport(self, resources: AsyncExitStack) -> UnboundToolTransport:
        transport = await super().open_transport(resources)
        self.contexts[self.command.input_id] = TrackedInputContext(self.command)
        return transport

    @classmethod
    async def execute(
        cls,
        package: VerifiedNativePackage,
        *,
        input_id: InputId,
        prompt: str,
        context_contributions: tuple[InputContributionCoordinates, ...] = (),
        worktree: Path,
        session: SelectedSession,
        model: SelectedModel,
        thinking_level: type[ThinkingChoice] = NoThinkingOverride,
        environment: Mapping[str, str] = MappingProxyType({}),
        model_wait: TurnWait = DEFAULT_MODEL_WAIT,
        prompt_send_boundary: PromptBoundary = UNGUARDED_PROMPT,
        ingress: MaintenanceIngress = UNCOORDINATED_INGRESS,
        startup: NativeStartup,
        selected_tool_mode: NativeToolMode,
        observe_event: NativeObserver = NO_NATIVE_OBSERVER,
        request_observer: RequestObserver = NO_REQUEST_OBSERVER,
        execution: NativeExecution,
        following: FollowingInputs = NO_FOLLOWING_INPUTS,
    ) -> NativeTurnResult:
        if not prompt or not isinstance(prompt, str):
            raise ValueError("A native turn requires a nonempty prompt")
        measurements = startup.measurements
        # Independent execution acquires a fresh artifact. A selected stage
        # derives from its execution's original pre-claim factory; source/config and
        # child permissions remain fresh. Finish construction before acquiring the child.
        with measurements.operation("native_launch_selection"):
            launch = await Coordination.run_worker(partial(
                execution.tracked_launch,
                package,
                root=startup.root,
                worktree=worktree,
                session=session,
                model=model,
                thinking_level=thinking_level,
                environment=environment,
                selected_tool_mode=selected_tool_mode,
            ))
        turn = cls(
            launch,
            partial(commands.Prompt,
                input_id=input_id,
                message=prompt,
                context_contributions=context_contributions,
            ),
            model=model,
            model_wait=model_wait,
            prompt_send_boundary=prompt_send_boundary,
            ingress=ingress,
            startup=startup,
            selected_tool_mode=selected_tool_mode,
            observe_event=observe_event,
            request_observer=request_observer,
            pi_session=execution.pi_session,
            cursor_delivery=execution.cursor_delivery,
            following=following,
        )
        return await turn.complete()

    async def complete(self) -> NativeTurnResult:
        async with AsyncExitStack() as ownership:
            retirement = self.startup.measurements.operation("native_child_retirement")
            # Exit this observation after every original resource callback.
            ownership.push(retirement)
            try:
                drafts = ExitStack()
                ownership.push_async_callback(Coordination.run_worker, drafts.close)
                # Transport resources are the turn's innermost lifetime. Stop
                # closes them before child cleanup can block, so an
                # interrupted selected turn cannot leave its socket path behind.
                async with AsyncExitStack() as transport:
                    self.native = await self.acquire_native(transport, reuse=True)
                    ownership.push_async_callback(self.retain_or_close)
                    self.draft_publication = await Coordination.run_worker(partial(
                        drafts.enter_context, DraftRow.publication(
                            self.startup.root, self.command.input_id)))
                    await ownership.enter_async_context(self.native.failures())
                    self.watchdog.reading()
                    try:
                        try:
                            await self.attest()
                            await self.send_prompt()
                            while not self.finished:
                                event = await self.next_event()
                                async for update in self.consume_native_event(event):
                                    await self.observe_event(update)
                                if not self.finished:
                                    await self.observe_event(event)
                            result = await self.result()
                            return result
                        except (SelectedToolDenied, PromptSendFailure, TimeoutError, OSError) as error:
                            raise NativePiUnavailable(
                                f"Native Pi operation failed: {type(error).__name__}: {error}"
                            ) from error
                    except NativePiUnavailable as error:
                        self.retainable = False
                        self.prompt_state.raise_native_failure(error, self.native.attestation)
            finally:
                # LIFO starts the measurement immediately before cleanup. No
                # await or manual close may separate this from stack retirement.
                ownership.callback(retirement.__enter__)

    async def retain_or_close(self) -> None:
        """Let the session keep a verified idle child; close any other child."""
        await self.finish_input()
        retention = SessionRetention.from_optional(self.native.attestation.identity)
        # An input written but never started may still run in the child; it is not idle.
        self.retainable = self.retainable and self.inputs.settled
        if self.retainable and retention.retain(self.native, self.native_session):
            return
        await self.native_session.close()

    async def next_event(self) -> pi.PiEvent:
        try:
            while True:
                refused, self.rejected_commands = self.rejected_commands, []
                self.rejected_signal.clear()
                for command in refused:
                    await self.following.retired(command)
                raw = await self.watchdog.read(self)
                if not self.rejected_commands:
                    break  # Otherwise the read was only woken to report a refused input.
            event = PiRpcChannel.decode_record(raw, strict=True) if raw else None
        except TurnWaitExpired as expired:
            async for update in expired.wait.expire(self):
                await self.observe_event(update)
            raise NativePiUnavailable(self.output.declared_failure.text) from expired
        except MalformedRecord as error:
            raise NativePiUnavailable(
                f"Native Pi RPC record is invalid or incomplete; {type(error).__name__}: {error}"
            ) from error
        if event is None:
            await self.native.shutdown(graceful=True)
            stderr = await self.native.proc.stderr_tail()
            raise NativePiUnavailable(
                f"Native Pi RPC record is incomplete; native exit={self.native.proc.returncode}; "
                f"stderr={stderr or '(empty)'}"
            )
        return event

    async def send(self, command: commands.PiCommand) -> None:
        try:
            async with self.ingress.ingress_async() as status:
                status.require_open()
                self.write(command)
        except RelationViolationError as error:
            raise NativePiUnavailable("Maintenance closed before Pi capability preflight") from error
        wait = self.watchdog.wait_for(self)
        with wait.charge(self.watchdog.clock):
            await asyncio.wait_for(self.native.drain(), timeout=wait.timeout())

    async def attest(self) -> None:
        self.native.attestation = self.launch.session.tracked_attestation(
            self.native.attestation
        )
        if self.native.attestation.observed:
            identity = self.native.attestation.require_identity()
            self.active_session_file = self.launch.session.attest(identity)
            return
        request = self.native.attestation.request
        with self.startup.measurements.operation("get_state_send"):
            await self.send(request)
        with self.startup.measurements.operation("get_state_receive"):
            while True:
                event = await self.next_event()
                try:
                    observed = self.native.attestation.accept(event)
                except AttestationError as error:
                    raise NativePiUnavailable(str(error)) from error
                if observed.observed:
                    if self.native.reader.correlate(event) is not request:
                        raise NativePiUnavailable("Native Pi preflight response is not owned by its request")
                    break
                async for _ in event.consume(self):
                    pass
        if observed.identity is None:
            raise NativePiUnavailable("Native Pi omitted its private session identity")
        self.active_session_file = self.launch.session.attest(observed.identity)
        self.native.attestation = observed
        with self.startup.measurements.operation("attestation_publication"):
            await self.observe_event(observed)

    def write(self, command: commands.PiCommand) -> None:
        if command is self.command:
            self.grant_prompt()
        self.native.send(command)

    async def send_prompt(self) -> None:
        await self.prompt_send_boundary.send_prompt(self)

    def reject_prompt(self, response):
        raise NativePiPromptRejected(response)

    def following_started(self, command: commands.FollowingPrompt) -> None:
        if command.input_id in self.contexts:
            raise NativePiUnavailable("A tracked following input reused an in-scope native identity")
        self.contexts[command.input_id] = TrackedInputContext(command)

    async def finish_input(self) -> None:
        try:
            await super().finish_input()
        finally:
            self.contexts.clear()

    @handles(pi.InputCommitted)
    async def committed_input(self, event: pi.InputCommitted) -> None:
        if context := self.contexts.get(event.input_id):
            await context.committed_input(self, event)

    def forward_inbox(self, inbox: TurnInbox, sender: OwnedSend) -> None:
        """Later inputs go to the running child under the owner's write checks."""
        self.inputs.queue = inbox
        self.send_boundary = sender
        self.input_start = sender
        self.ensure_input_forwarding()

    async def accept_user_message(
        self, message: UserMessage, event: pi.MessageStart
    ) -> tuple[AgentEvent, ...]:
        """The original input starts when Pi commits it; any other user message is a later input."""
        if message.matches_input(self.command.message, self.command.input_id):
            return ()
        for input_id in self.inputs.mark_started(self, event):
            source = await self.following.started(input_id)
            self.terminal = source.continue_response(self.terminal, self.output.text_parts)
            self.output.discard_text()
            return ()
        raise NativePiUnavailable("Native Pi started a user message this turn did not send")

    @handles(pi.ContextCommitted)
    async def committed_context(self, event: pi.ContextCommitted) -> None:
        if context := self.contexts.get(event.input_id):
            await context.committed_context(self, event)

    async def assistant_started(self) -> None:
        await super().assistant_started()
        self.draft_written_at = 0.0
        self.draft_publication.begin_message(time.time())

    async def text_received(self, text: str) -> None:
        await super().text_received(text)
        now = time.monotonic()
        if now - self.draft_written_at >= .1:
            await Coordination.run_worker(partial(
                self.draft_publication.publish, tuple(self.output.message_parts)))
            self.draft_written_at = now

    async def assistant_ended(self) -> None:
        await Coordination.run_worker(self.draft_publication.retire_message)

    def accept_tool_round(self, message):
        super().accept_tool_round(message)
        self.output.discard_text()
        self.terminal = self.terminal.tool_round()

    def accept_unfinished_message(self, failure: TurnFailure) -> None:
        self.output.record_failure(failure)

    def record_retry(self, result: ProviderRetryResult) -> None:
        """A tracked failure proves its terminal; it grants no provider retry permission."""

    def accept_final_message(self, message):
        self.tool_socket.assert_complete()
        self.terminal = self.terminal.append(message.authoritative_text)

    async def context_proof(self) -> NativeContextProof:
        if not self.prompt_state.acknowledged:
            raise NativePiUnavailable("Native Pi did not commit a tracked model context")
        # The recipient serializes this read with cursor and settlement reads.
        # Cancellation joins the worker before its borrowed scope is released.
        async with self.cursor_delivery.reading() as source_reads:
            return await Coordination.run_worker(partial(
                self.contexts[self.command.input_id].proof,
                self.launch.session.files(self.launch.root),
                self.native.attestation.require_identity().session_id, source_reads,
            ))

    async def invalidate_identity(self) -> tuple[AgentEvent, ...]:
        # A tracked turn has no authority to recover a session another command replaced.
        raise NativePiUnavailable("Native Pi session identity changed during a turn")

    async def result(self) -> NativeTurnResult:
        with self.startup.measurements.operation("native_terminal_result"):
            with self.startup.measurements.operation("native_terminal_proof"):
                proof = await self.context_proof()
            self.output.raise_if_failed(proof, self.model)
            self.tool_socket.assert_complete()
            # An empty model completion does not make a proved, settled child unavailable.
            # Per-input tool sockets still determine whether that child can be reused.
            self.retainable = self.tool_socket.retainable
            response = self.terminal.require_response(self.output.text_parts)
            return NativeTurnResult(
                response.strip(),
                proof,
            )
