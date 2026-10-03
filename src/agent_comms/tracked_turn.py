"""Tracked native turns share Pi declarations and the existing turn-session authority."""

from __future__ import annotations

import asyncio
import math
import os
import re
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable
from contextlib import AsyncExitStack, contextmanager
from dataclasses import dataclass
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING

from . import pi_commands as commands
from . import pi_events as pi
from .agent_events import AgentEvent
from .backend import MODEL_WAIT_TIMEOUT_SECONDS, TurnSession
from .errors import RelationViolationError
from .maintenance_barrier import MaintenanceBarrier
from .mro_dispatch import MroDispatch, handles
from .native_attestation import AttestationError, ObservedAttestation
from .native_pi import (
    NativeContextProof,
    NativePiPromptRejected,
    NativePiRpcLaunch,
    NativePiTerminalFailure,
    NativePiUnavailable,
    NativeTurnResult,
    _verify_context,
)
from .native_prompt_send import PromptSendFailure, send_fenced_prompt
from .native_entries import NativeEntry
from .native_startup import NativeStartupAdmission
from .diagnostics import PublicationMeasurements
from .child_process import ProcessIdentity
from .coordinator import Coordination
from .request_progress import RequestProgress
from .native_tool_call import SelectedToolDenied
from .pi_payloads import TextDelta
from .pi_rpc import PiRpcChannel
from .selected_tool_broker import NativeToolMode, OwnerToolSocket
from .selected_session import SelectedSession
from .turn_context import InputContributionCoordinates


if TYPE_CHECKING:
    from .private_send_admission import PrivateSendAdmission


class NativeCommitObservation[T](ABC):
    """Observed original native receipts, without granting admission or replay."""

    observed = False

    @abstractmethod
    def require(self) -> T: ...

    def capture(self, event: T) -> NativeCommitObservation[T]:
        raise NativePiUnavailable("Native Pi repeated the input commitment")


class PendingNativeCommit[T](NativeCommitObservation[T]):
    def require(self) -> T:
        raise NativePiUnavailable("Native Pi did not commit a tracked model context")

    def capture(self, event: T) -> NativeCommitObservation[T]:
        return ObservedNativeCommit(event)


@dataclass(frozen=True)
class ObservedNativeCommit[T](NativeCommitObservation[T]):
    event: T
    observed = True

    def require(self) -> T:
        return self.event


class TrackedTerminal(ABC):
    """Terminal data owns failure, missing, unique and ambiguous observations."""

    def append(self, text: str) -> TrackedTerminal:
        return CompletedTrackedTerminal(text)

    def fail(self, text: str) -> TrackedTerminal:
        return FailedTrackedTerminal(text)

    def tool_round(self) -> TrackedTerminal:
        return PendingTrackedTerminal()

    def raise_failure(self, proof, provider, model) -> None:
        pass

    @abstractmethod
    def require_response(self, parts: list[str]) -> str: ...


class PendingTrackedTerminal(TrackedTerminal):
    def require_response(self, parts):
        raise NativePiUnavailable("Native Pi has no unique authoritative completed response")


@dataclass(frozen=True)
class CompletedTrackedTerminal(TrackedTerminal):
    text: str

    def append(self, text):
        return AmbiguousTrackedTerminal()

    def require_response(self, parts):
        if not self.text or "".join(parts) != self.text:
            raise NativePiUnavailable("Native Pi has no unique authoritative completed response")
        return self.text


class AmbiguousTrackedTerminal(PendingTrackedTerminal):
    def append(self, text):
        return self


@dataclass(frozen=True)
class FailedTrackedTerminal(TrackedTerminal):
    text: str

    def append(self, text):
        return self

    def tool_round(self):
        return self

    def raise_failure(self, proof, provider, model):
        raise NativePiTerminalFailure(self.text, proof, provider, model)

    def require_response(self, parts):
        raise NativePiUnavailable("Native Pi has no unique authoritative completed response")


class TrackedTurnSession(TurnSession, MroDispatch):
    """Own one strict input/proof exchange; never grant publication or retry authority."""

    def __init__(
        self,
        launch,
        command,
        *,
        provider,
        model,
        model_wait_timeout,
        prompt_send_boundary,
        maintenance_root,
        startup: NativeStartupAdmission,
        selected_tool_mode,
        observe_event,
        request_observer,
    ):
        super().__init__(
            launch,
            command.message,
            model_wait_timeout=model_wait_timeout,
            startup=startup,
            request_observer=request_observer,
        )
        self.command = command
        self.provider, self.model = provider, model
        self.prompt_send_boundary = prompt_send_boundary
        self.maintenance_root = maintenance_root
        self.selected_tool_mode, self.observe_event = selected_tool_mode, observe_event
        self.tool_socket: OwnerToolSocket | None = None
        self.input_commit: NativeCommitObservation[pi.InputCommitted] = PendingNativeCommit()
        self.context_commit: NativeCommitObservation[pi.ContextCommitted] = PendingNativeCommit()
        self.text_parts: list[str] = []
        self.terminal: TrackedTerminal = PendingTrackedTerminal()
        self.finished = False

    @classmethod
    async def execute(
        cls,
        package: Path,
        *,
        input_id: str,
        prompt: str,
        context_contributions: tuple[InputContributionCoordinates, ...] = (),
        worktree: Path,
        session: SelectedSession,
        provider: str = "openrouter",
        model: str = "z-ai/glm-5.3-flash",
        thinking_level: str | None = None,
        environment: dict[str, str] | None = None,
        model_wait_timeout: float | None = MODEL_WAIT_TIMEOUT_SECONDS,
        prompt_send_boundary: PrivateSendAdmission | None = None,
        maintenance_root: Path | None = None,
        selected_tool_mode: NativeToolMode | None = None,
        observe_event: Callable[[pi.PiEvent | AgentEvent | ObservedAttestation], Awaitable[None]] | None = None,
        acquisition_measurements: PublicationMeasurements | None = None,
        request_observer: Callable[[RequestProgress, ProcessIdentity], None] | None = None,
    ) -> NativeTurnResult:
        if type(input_id) is not str or re.fullmatch(r"[0-9a-f]{32}", input_id) is None:
            raise ValueError("A native turn requires a 128-bit lowercase hex input ID")
        if not prompt or not isinstance(prompt, str):
            raise ValueError("A native turn requires a nonempty prompt")
        if model_wait_timeout is not None and (
            not math.isfinite(model_wait_timeout) or model_wait_timeout <= 0
        ):
            raise ValueError("A model progress wait must be positive and finite")
        measurements = (acquisition_measurements if acquisition_measurements is not None
                        else PublicationMeasurements())
        # This fresh child still acquires its own verified launch. Hashing and
        # settings publication must not block the native event reader; joined
        # cancellation completes that acquisition before returning to custody.
        with measurements.operation("native_launch_selection"):
            launch = await Coordination.run_worker(partial(
                NativePiRpcLaunch.tracked,
                package,
                worktree=worktree,
                session=session,
                provider=provider,
                model=model,
                thinking_level=thinking_level,
                environment=environment,
                selected_tool_mode=selected_tool_mode,
            ))
        turn = cls(
            launch,
            commands.Prompt(
                id="native-prompt",
                input_id=input_id,
                message=prompt,
                context_contributions=context_contributions,
            ),
            provider=provider,
            model=model,
            model_wait_timeout=model_wait_timeout,
            prompt_send_boundary=prompt_send_boundary,
            maintenance_root=maintenance_root,
            startup=session.startup_admission(launch, maintenance_root, prompt_send_boundary,
                                             measurements=measurements),
            selected_tool_mode=selected_tool_mode,
            observe_event=observe_event,
            request_observer=request_observer,
        )
        return await turn.complete()

    async def complete(self) -> NativeTurnResult:
        async with AsyncExitStack() as custody:
            retirement = self.startup.measurements.operation("native_custody_retirement")
            # Exit this observation after every original resource callback.
            custody.push(retirement)
            try:
                self.custody = custody
                self.native = await self.acquire_native(custody, reuse=False)
                custody.push_async_callback(self.native_session.close)
                await custody.enter_async_context(self.native.failures())
                custody.callback(self.native.reader.pending.cancel_all)
                if self.tool_socket is not None:
                    self.tool_socket.expected_pid = self.native.proc.pid
                self.watchdog.reading()
                try:
                    try:
                        await self.attest()
                        if self.prompt_send_boundary is not None:
                            with self.startup.measurements.operation("selected_context_preparation"):
                                await self.prompt_send_boundary.prepare_context(self)
                        await self.admit_prompt()
                        while not self.finished:
                            event = await self.next_event()
                            async for update in self.consume_native_event(event):
                                if self.observe_event is not None:
                                    await self.observe_event(update)
                            if not self.finished and self.observe_event is not None:
                                await self.observe_event(event)
                        return await self.result()
                    except (SelectedToolDenied, PromptSendFailure, TimeoutError, OSError) as error:
                        raise NativePiUnavailable(
                            f"Native Pi operation failed: {type(error).__name__}: {error}"
                        ) from error
                except NativePiUnavailable as error:
                    self.admission.raise_native_failure(error, self.native.attestation)
            finally:
                # LIFO starts the measurement immediately before cleanup. No
                # await or manual close may separate this from stack retirement.
                custody.callback(retirement.__enter__)

    async def resume_prepared(self, resources: AsyncExitStack) -> None:
        await super().resume_prepared(resources)
        if self.tool_socket is not None:
            self.tool_socket.expected_pid = self.native.proc.pid
        self.watchdog.reading()
        await self.attest()

    async def open_transport(self, custody: AsyncExitStack) -> None:
        if self.selected_tool_mode is not None:
            self.tool_socket = self.selected_tool_mode.socket(
                self.launch.session.directory, os.urandom(32).hex()
            )
            custody.push_async_callback(self.tool_socket.close)
            await self.tool_socket.start()
            self.launch.env["AGENT_COMMS_SELECTED_TOOL_SOCKET"] = str(self.tool_socket.address)
            self.launch.env["AGENT_COMMS_SELECTED_TOOL_TOKEN"] = self.tool_socket.token

    @property
    def started_input(self):
        return self.input_commit.observed

    @property
    def active_tools(self):
        return self.tool_socket.active_tools if self.tool_socket is not None else set()

    async def apply_native_event(self, event):
        await self.dispatch(event)
        if False:
            yield

    async def next_event(self) -> pi.PiEvent:
        try:
            raw = await self.watchdog.read(self)
            event = PiRpcChannel.decode_record(raw, strict=True) if raw else None
        except (UnicodeError, ValueError, TypeError) as error:
            raise NativePiUnavailable(
                f"Native Pi RPC record is invalid or incomplete; {type(error).__name__}: {error}"
            ) from error
        if event is None:
            await self.native.proc.finish()
            stderr = await self.native.stderr_task
            raise NativePiUnavailable(
                f"Native Pi RPC record is incomplete; native exit={self.native.proc.returncode}; "
                f"stderr={stderr or '(empty)'}"
            )
        return event

    async def send(self, command: commands.PiCommand) -> None:
        payload = self.native.reader.encode(command)
        if self.maintenance_root is not None:
            try:
                async with MaintenanceBarrier(
                    self.maintenance_root / "registry.json"
                ).admit_ingress_async():
                    self.write(command, payload)
            except RelationViolationError as error:
                raise NativePiUnavailable(
                    "Maintenance closed before Pi capability preflight"
                ) from error
        else:
            self.write(command, payload)
        await asyncio.wait_for(
            self.native.proc.stdin.drain(), timeout=self.watchdog.read_timeout(self)
        )

    async def attest(self) -> None:
        request = self.native.attestation.request
        with self.startup.measurements.operation("get_state_send"):
            await self.send(request)
        with self.startup.measurements.operation("get_state_receive"):
            event = await self.next_event()
        if not isinstance(event, pi.Response) or self.native.reader.correlate(event) is not request:
            raise NativePiUnavailable("Native Pi emitted an unexpected preflight event")
        try:
            observed = self.native.attestation.accept(event)
        except AttestationError as error:
            raise NativePiUnavailable(str(error)) from error
        state = observed.state
        if observed.identity is None:
            raise NativePiUnavailable("Native Pi omitted its private session identity")
        self.active_session_file = self.launch.session.attest(observed.identity)
        self.native.attestation = observed
        self.startup.release()
        self.startup.attest(state)
        if self.observe_event is not None:
            with self.startup.measurements.operation("attestation_publication"):
                await self.observe_event(observed)

    def write(self, command: commands.PiCommand, payload: bytes) -> None:
        if command is self.command:
            self.grant_prompt()
        self.native.proc.stdin.write(payload)

    @contextmanager
    def prompt_boundary(self):
        with self.startup.prompt_boundary(self.prompt_send_boundary, self.native.attestation.identity):
            # The isolated writer alone reaches this original granted boundary.
            # The awaiting owner cannot consume native events until it is joined.
            self.grant_prompt()
            yield

    async def admit_prompt(self) -> None:
        if self.prompt_send_boundary is None:
            await self.send(self.command)
        else:
            with self.startup.measurements.operation("prompt_writer_join"):
                await send_fenced_prompt(
                    self.native.proc.stdin,
                    self.native.reader.encode(self.command),
                    self.prompt_boundary,
                    measurements=self.startup.measurements,
                )

    @handles(pi.Response)
    async def response(self, event: pi.Response) -> None:
        request = self.native.reader.correlate(event)
        if event.id == self.command.id:
            if request is not self.command:
                raise NativePiUnavailable("Native Pi prompt acknowledgement differs")
            if event.success is not True:
                raise NativePiPromptRejected(event)
            self.admission = self.admission.acknowledge(event)
        elif issubclass(event.command, commands.MutatesSession):
            raise NativePiUnavailable("Native Pi session identity changed during a turn")

    @handles(pi.InputCommitted)
    async def committed_input(self, event: pi.InputCommitted) -> None:
        if event.input_id == self.command.input_id:
            self.input_commit = self.input_commit.capture(event)
            self.evidence = self.custody.enter_context(
                NativeEntry.open_input_evidence(self.active_session_file)
            )
            await asyncio.to_thread(self.evidence.observe)

    @handles(pi.ContextCommitted)
    async def committed_context(self, event: pi.ContextCommitted) -> None:
        if event.input_id == self.command.input_id:
            self.context_commit = ObservedNativeCommit(event)

    @handles(pi.MessageUpdate)
    async def message_update(self, event: pi.MessageUpdate) -> None:
        if event.assistant_message_event is not None:
            await self.dispatch(event.assistant_message_event)

    @handles(TextDelta)
    async def text_delta(self, delta: TextDelta) -> None:
        self.text_parts.append(delta.delta)

    @handles(pi.MessageEnd)
    async def message_end(self, event: pi.MessageEnd) -> None:
        event.message.tracked_end(self)

    def accept_tool_round(self, message):
        if self.tool_socket is None:
            return False
        self.tool_socket.announce(message.content)
        self.text_parts.clear()
        self.terminal = self.terminal.tool_round()
        return True

    def accept_final_message(self, message):
        if self.tool_socket is not None:
            self.tool_socket.assert_complete()
        self.terminal = self.terminal.append(message.authoritative_text)

    def fail_terminal(self, text: str):
        self.terminal = self.terminal.fail(text)

    async def context_proof(self) -> NativeContextProof:
        if not self.admission.acknowledged:
            raise NativePiUnavailable("Native Pi did not commit a tracked model context")
        # The original event consumer serializes observations. Cancellation
        # joins this read before the acquired evidence/custody can close.
        return await Coordination.run_worker(partial(_verify_context,
            self.active_session_file,
            self.command.input_id,
            self.native.attestation.identity.session_id,
            self.input_commit.require(),
            self.context_commit.require(),
            evidence=self.evidence,
        ))

    @handles(pi.ToolExecutionStart)
    async def tool_started(self, event: pi.ToolExecutionStart) -> None:
        if self.tool_socket is None:
            raise NativePiUnavailable("Native Pi tool preceded tracked context proof")
        await self.context_proof()
        self.tool_socket.tool_started(event)

    @handles(pi.ToolExecutionEnd)
    async def tool_finished(self, event: pi.ToolExecutionEnd) -> None:
        if self.tool_socket is None:
            raise NativePiUnavailable("Native Pi tool has no owner policy")
        self.tool_socket.tool_finished(event, self.command.input_id)

    @handles(pi.AgentSettled)
    async def settled(self, event: pi.AgentSettled) -> None:
        with self.startup.measurements.operation("native_agent_settled"):
            self.finished = True

    async def result(self) -> NativeTurnResult:
        with self.startup.measurements.operation("native_terminal_result"):
            with self.startup.measurements.operation("native_terminal_proof"):
                proof = await self.context_proof()
            self.terminal.raise_failure(proof, self.provider, self.model)
            if self.tool_socket is not None:
                self.tool_socket.assert_complete()
            response = self.terminal.require_response(self.text_parts)
            if self.selected_tool_mode is not None:
                await self.selected_tool_mode.finish()
            return NativeTurnResult(
                response.strip(),
                proof,
                self.tool_socket.selected_call_id if self.tool_socket else None,
            )
