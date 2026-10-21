"""Pi native turns: the private turn implementation behind ``PiNativeBackend``.

Core reaches this module only through ``PiNativeBackend.run_turn``. It runs the
selected native Pi process through its verified RPC launch contract. Pi's
JSON-lines events carry text deltas, tool arguments and tool results for
thinking indicators and tool-call cards.

Events are frozen nominal values declared in :mod:`agent_events`.

Native terminal failures yield ``done`` with ``ok=False``. Unexpected producer
faults retain their original exception for the existing owner-turn failure
publisher after child retirement. Provider failures reported as an assistant
message also fail the turn, carrying ``errorMessage`` and typed diagnostics.
"""

from __future__ import annotations

from agent_comms.participant_identity import ParticipantIdentity

import asyncio
import json
import secrets
import unicodedata
from abc import ABC, abstractmethod
from collections.abc import (
    AsyncGenerator,
    AsyncIterator,
    Awaitable,
    Callable,
    Iterable,
    Iterator,
    Sequence,
)
from contextlib import AsyncExitStack, aclosing, contextmanager
from dataclasses import dataclass
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar

from agent_comms.pi_payloads import RequestId

from . import agent_events as events
from . import pi_commands as commands
from . import pi_events as pi
from . import turn_failure as failures
from .agent_backend import TurnInbox
from .acp_failure import UnrecordedFailureSource
from .child_process import TimedOutOutcome
from .coordinator import Coordination
from .diagnostics import FailureReason
from .extension_ui import ExtensionUiSession
from .field_codec import Id128Text, MalformedRecord, boundary_decoder
from .image_inputs import ImageInput
from .native_attestation import AttestationError
from .native_child import PiSessionChild
from .native_package import VerifiedNativePackage
from .native_pi import NativePiRpcLaunch
from .native_failures import NativePiUnavailable
from .native_session_reopen import SessionRetention
from .native_startup import NATIVE_STARTUP_POLICY, NativeStartup
from .pi_rpc import PiRpcChannel
from .prompt_state import UnwrittenPrompt
from .request_progress import NO_REQUEST_OBSERVER, RequestObserver, RequestProgress
from .native_tool_call import NativeToolMode
from .native_tool_transport import ToolTransport, UnboundToolTransport
from .mro_dispatch import MroDispatch, handles
from .selected_tool_broker import SelectedToolDenied
from .send_outcome import SendBoundary
from .turn_context import InputContributionCoordinates
from .turn_inputs import InputForwarding
from .turn_output import TurnOutput
from .turn_phase import SettlingStatsPhase, ShutdownPhase, TurnPhase
from .turn_stats import StatsRequest
from .turn_usage import UsageAccount
from .turn_watchdog import DEFAULT_MODEL_WAIT, ProgressWatchdog, TurnWait, TurnWaitExpired

if TYPE_CHECKING:
    from .acp_failure import ProviderRetryResult
    from .pi_native_backend import PersistentPiSession
    from .pi_payloads import AssistantMessage, UserMessage


def compaction_summary(value: Any) -> str:
    """Preserve saved Markdown, excluding terminal control codes."""
    if not isinstance(value, str):
        return ""
    return "".join(
        char if char in "\n\t" or not unicodedata.category(char).startswith("C") else " "
        for char in value.replace("\r\n", "\n")
    )


# Pi 0.85.1 owns provider-idle detection and defaults it to 300 seconds. This
# transport backstop must remain strictly longer so Pi can emit its authoritative
# timeout, auto-retry, and transport evidence before agent-comms intervenes.
_PI_0_85_1_PROVIDER_IDLE_TIMEOUT_SECONDS = 300.0
assert DEFAULT_MODEL_WAIT.budget > _PI_0_85_1_PROVIDER_IDLE_TIMEOUT_SECONDS
RPC_ABORT_GRACE_SECONDS = 2.0
CAPABILITY_PREFLIGHT_TIMEOUT_SECONDS = NATIVE_STARTUP_POLICY.readiness_seconds
PROMPT_START_TIMEOUT_SECONDS = 180.0
_IDENTITY_FAILURE_TEXT = "Pi session identity changed during this turn."


async def terminate_task_process(task: asyncio.Task[Any]) -> None:
    """Terminate the backend subprocess owned by an agent turn task."""
    session = TurnSession.active.get(task)
    if session is not None:
        await session.native_session.close()
        TurnSession.active.pop(task, None)


async def stream_agent_events(
    agent_bin: str,
    agent_args: Sequence[str],
    task: str,
    cwd: str,
    env_extra: dict[str, str] | None = None,
    session_file: str | None = None,
    steering_queue: TurnInbox | None = None,
    finish_event: asyncio.Event | None = None,
    images: Sequence[ImageInput] = (),
    model_wait: TurnWait = DEFAULT_MODEL_WAIT,
    rpc_abort_grace: float = RPC_ABORT_GRACE_SECONDS,
    send_boundary: SendBoundary | None = None,
    interrupt_boundary: SendBoundary | None = None,
    native_start: Callable[[str | None, str, str], bool] | None = None,
    *,
    persistent_session: PersistentPiSession,
    package: VerifiedNativePackage,
    root: Path,
    owner: ParticipantIdentity,
    session_directory: Path,
    ui_request: Callable[[pi.DialogUiRequest], Awaitable[pi.ExtensionUiChoice]] | None = None,
    context_contributions: tuple[InputContributionCoordinates, ...] = (),
    request_observer: RequestObserver = NO_REQUEST_OBSERVER,
    tool_mode: NativeToolMode,
) -> AsyncIterator[events.AgentEvent]:
    """Run the backend; native completion ends with a ``done`` event.

    The no-progress watchdog applies only while waiting for the model. A running
    tool has no deadline in this intentionally incomplete first slice: preventing
    false model-stall kills does not solve a hung tool, which requires a separate
    durable tool-liveness policy. Durable retry decisions belong to the
    coordinator, never this transport adapter.
    """
    async with persistent_session.lock:
        try:
            launch = await Coordination.run_worker(partial(
                persistent_session.child_state.managed_launch,
                agent_bin,
                tuple(agent_args),
                package=package,
                root=root,
                owner=owner,
                directory=session_directory,
                worktree=Path(cwd),
                environment=env_extra,
                session_file=session_file,
            ))
        except NativePiUnavailable as error:
            yield events.Error(text=str(error), reason_code="native_launch_invalid")
            yield events.Done(ok=False, reason_code="native_launch_invalid", text=str(error))
            return
        from .session_fence import session_writer_fence

        async with session_writer_fence(launch.session.session_file):
            async with aclosing(
                TurnSession(
                    launch,
                    partial(commands.Prompt,
                        id=RequestId(f"agent-comms-prompt-{secrets.token_hex(16)}"),
                        input_id=Id128Text.mint(), message=task, images=tuple(images),
                        context_contributions=context_contributions,
                    ),
                    tool_mode=tool_mode,
                    steering_queue=steering_queue,
                    finish_signal=FinishSignal.from_event(finish_event),
                    model_wait=model_wait,
                    rpc_abort_grace=rpc_abort_grace,
                    send_boundary=send_boundary,
                    input_start=InputStartListener.from_callback(native_start),
                    interrupt_boundary=interrupt_boundary,
                    native_session=persistent_session,
                    ui_request=ui_request,
                    request_observer=request_observer,
                    startup=NativeStartup.for_launch(launch),
                ).run()
            ) as stream:
                async for event in stream:
                    yield event


class InputStartListener(ABC):
    """The listener owns acknowledgement and the corresponding start notification."""

    started_events: tuple[events.AgentEvent, ...]

    @classmethod
    @boundary_decoder
    def from_callback(
        cls, report: Callable[[str | None, str, str], bool] | None,
    ) -> InputStartListener:
        if report is None:
            return NoInputStartListener()
        return ReportInputStart(partial(report, None), report)

    @abstractmethod
    def original(self, native_id: str, text: str) -> bool: ...

    @abstractmethod
    def following(self, public_id: str, native_id: str, text: str) -> bool: ...


class NoInputStartListener(InputStartListener):
    started_events = ()

    def original(self, native_id: str, text: str) -> bool:
        return True

    def following(self, public_id: str, native_id: str, text: str) -> bool:
        return True


@dataclass(frozen=True)
class ReportInputStart(InputStartListener):
    on_original: Callable[[str, str], bool]
    on_following: Callable[[str, str, str], bool]
    started_events = (events.OriginalInputStarted(),)

    def original(self, native_id: str, text: str) -> bool:
        return self.on_original(native_id, text)

    def following(self, public_id: str, native_id: str, text: str) -> bool:
        return self.on_following(public_id, native_id, text)


NO_INPUT_START_LISTENER = NoInputStartListener()


class FinishSignal(ABC):
    """Own the optional external finish wait, without a nullable task in the reader."""

    @classmethod
    @boundary_decoder
    def from_event(cls, event: asyncio.Event | None) -> FinishSignal:
        return NoFinishSignal() if event is None else EventFinishSignal(event)

    @abstractmethod
    def waiters(self, *, settling: bool) -> tuple[asyncio.Task[bool], ...]: ...


class NoFinishSignal(FinishSignal):
    def waiters(self, *, settling: bool) -> tuple[asyncio.Task[bool], ...]:
        return ()


@dataclass(frozen=True)
class EventFinishSignal(FinishSignal):
    event: asyncio.Event

    def waiters(self, *, settling: bool) -> tuple[asyncio.Task[bool], ...]:
        return () if settling else (asyncio.create_task(self.event.wait()),)


NO_FINISH_SIGNAL = NoFinishSignal()


class NativeSession(MroDispatch, ABC):
    """Shared child acquisition, attestation, observation and retirement."""

    active: ClassVar[dict[asyncio.Task[Any], NativeSession]] = {}

    @property
    @abstractmethod
    def prompt_type(self):
        """Each concrete native operation declares whether it sends input."""

    def __init__(
        self,
        launch: NativePiRpcLaunch,
        steering_queue: TurnInbox | None = None,
        finish_signal: FinishSignal = NO_FINISH_SIGNAL,
        model_wait: TurnWait = DEFAULT_MODEL_WAIT,
        rpc_abort_grace: float = RPC_ABORT_GRACE_SECONDS,
        send_boundary: SendBoundary | None = None,
        interrupt_boundary: SendBoundary | None = None,
        input_start: InputStartListener = NO_INPUT_START_LISTENER,
        ui_request: Callable[[pi.DialogUiRequest], Awaitable[pi.ExtensionUiChoice]] | None = None,
        request_observer: RequestObserver = NO_REQUEST_OBSERVER,
        *, startup: NativeStartup, native_session: PersistentPiSession,
    ):
        self.launch = launch
        self.tool_socket: ToolTransport
        self.finish_signal = finish_signal
        self.request_observer = request_observer
        self.watchdog = ProgressWatchdog(
            model_wait, PROMPT_START_TIMEOUT_SECONDS, CAPABILITY_PREFLIGHT_TIMEOUT_SECONDS,
            NATIVE_STARTUP_POLICY,
        )
        self.rpc_abort_grace = rpc_abort_grace
        self.send_boundary = send_boundary
        self.interrupt_boundary = interrupt_boundary
        self.input_start = input_start
        self.native_session = native_session
        self.extension_ui = ExtensionUiSession(ui_request)
        self.startup = startup
        self.inputs = InputForwarding(steering_queue if steering_queue is not None else TurnInbox())
        self.prompt_state = self.prompt_type()
        self.stats = StatsRequest()
        self.usage = UsageAccount()
        self.output = TurnOutput()
        self.rejected_signal = asyncio.Event()
        self.rejected_commands: list[events.InputRefused] = []
        self.explicit_interrupt = False
        self.settlement_count = 0
        self.started_during_abort: list[events.FollowingInputStarted] = []
        self.finished = self.skip = False

    def accept_tool_round(self, message):
        self.tool_socket.announce(message.content)

    def accept_final_message(self, message: AssistantMessage) -> None:
        """A turn that only streams its output takes no answer from the final message."""

    def accept_unfinished_message(self, failure: failures.TurnFailure) -> None:
        """A streaming turn reports an unfinished message through its output, not a failure."""

    async def assistant_started(self) -> None:
        self.output.start_message()

    async def assistant_ended(self) -> None:
        """A streaming turn keeps no per-message draft."""

    async def text_received(self, text: str) -> None:
        self.output.append(text)

    async def agent_settled(self, event: pi.AgentSettled) -> tuple[events.AgentEvent, ...]:
        """Settling asks Pi for the turn's statistics before the turn reports itself finished."""
        self.settlement_count += 1
        if self.inputs.pending:
            self.skip = True
            return ()
        self.prompt_state = self.prompt_state.settle(event)
        if not self.stats.requested:
            self.watchdog.tick()
            self.watchdog.progress()
            self.watchdog.phase = SettlingStatsPhase()
            await self.stats.request(self)
        return ()

    def native_phase_changes(self, previous: TurnPhase) -> Iterator[events.NativePhaseChanged]:
        """Publish the actual observer phase without storing another phase copy."""
        if self.watchdog.phase != previous:
            yield events.NativePhaseChanged(self.watchdog.phase, self.native.proc.identity)

    @property
    def started_input(self):
        return self.prompt_state.started and self.inputs.permits_interaction

    @property
    def awaiting_native_attestation(self):
        return not self.native.attestation.observed

    async def consume_native_event(self, event):
        """Observe one decoded event through the shared native lifecycle owner."""
        event.observe_request(self.record_request_progress)
        previous = self.watchdog.phase
        async for update in self.watchdog.observe(event, self):
            yield update
        for update in await self.apply_native_event(event):
            yield update
        self.watchdog.transition(event)
        for update in self.native_phase_changes(previous):
            yield update

    def record_request_progress(self, progress: RequestProgress) -> None:
        """Record the original sample without borrowing global publication locks.

        The acquired native process supplies its identity; the configured
        observer binds the original public turn lease. No phase owns a copy of
        the transport clock or request lifecycle.
        """
        self.request_observer(progress, self.native.proc.identity)

    async def apply_native_event(self, event) -> tuple[events.AgentEvent, ...]:
        if await self.dispatch(event) is None:
            return ()
        return tuple([update async for update in event.apply(self)])

    @property
    def accepts_output(self):
        return self.native.attestation.trustworthy

    def committable_message(self, text):
        return self.started_input and self.accepts_output and self.output.matches_message(text)

    def ensure_input_forwarding(self):
        if self.native.proc.stdin is not None:
            self.stdin = self.native.proc.stdin
            self.native.forward_input(partial(self.inputs.forward, self))

    def observe_input_during_abort(self, event):
        if self.accepts_output:
            matched, identity = self.inputs.mark_started(self, event)
            if matched:
                self.started_during_abort.append(events.FollowingInputStarted(id=identity))

    def permits_live_status(self) -> bool:
        """Only tracked, live input may publish an informational package acknowledgement."""
        if self.stats.requested:
            return False
        return self.prompt_state.permits_extension_ui(self)

    async def abort_stalled_rpc(self) -> None:
        await self.native.finish_input()
        if self.native.proc.stdin is not None and self.native.proc.returncode is None:
            abort_deadline = self.loop.time() + max(0.0, self.rpc_abort_grace)
            try:
                self.native.proc.stdin.write(
                    self.native.reader.encode(commands.Abort(id=RequestId("agent-comms-watchdog")))
                )
                remaining = max(0.0, abort_deadline - self.loop.time())
                await asyncio.wait_for(self.native.proc.stdin.drain(), timeout=remaining)
                while remaining := max(0.0, abort_deadline - self.loop.time()):
                    response = await asyncio.wait_for(
                        self.native.reader.readline(), timeout=remaining
                    )
                    if not response:
                        break
                    try:
                        abort_payload = PiRpcChannel.decode_record(response)
                    except (json.JSONDecodeError, UnicodeDecodeError, MalformedRecord):
                        continue
                    abort_payload.observe_abort(self)
                    if (
                        isinstance(abort_payload, pi.Response)
                        and abort_payload.command is commands.Abort
                    ):
                        break
            except TimeoutError:
                pass
        await self.native.shutdown()

    def context_info(self) -> events.ContextInfo:
        return events.ContextInfo(
            context_used=self.usage.used,
            context_size=self.usage.size,
        )

    async def open_transport(self, resources: AsyncExitStack) -> UnboundToolTransport:
        return await ToolTransport.open(self, resources)

    @contextmanager
    def native_acquisition(self):
        """The original acquisition failures preserve their source disposition."""
        try:
            yield
        except NativePiUnavailable as error:
            self.prompt_state.raise_native_failure(error, self.launch.session.attestation())

    async def acquire_native(self, resources: AsyncExitStack, *, reuse: bool) -> PiSessionChild:
        """Acquire leaf transport once in the original turn's resource lifetime."""
        with self.native_acquisition():
            async with AsyncExitStack() as transport:
                with self.startup.measurements.operation("open_transport"):
                    unbound = await self.open_transport(transport)
                native = await self.open_native(reuse=reuse)
                self.tool_socket = await unbound.bind_process(native.proc.pid)
                native.transport = transport.pop_all()
        self.owner = asyncio.current_task()
        if self.owner is not None:
            self.active[self.owner] = self
            resources.callback(self.active.pop, self.owner, None)
        return native

    async def open_native(self, *, reuse: bool) -> PiSessionChild:
        """Let the session manage child ownership for new or continued input."""
        with self.startup.measurements.operation("native_open"):
            return await self.native_session.open(
                self.launch, reuse=reuse, startup=self.startup, watchdog=self.watchdog,
            )

    async def run(self) -> AsyncGenerator[events.AgentEvent, None]:
        self.loop = asyncio.get_running_loop()
        self.owner = asyncio.current_task()
        async with AsyncExitStack() as resources:
            try:
                self.native = await self.acquire_native(
                    resources, reuse=True
                )
                async with self.native.failures():
                    self.output.sensitive |= self.native.sensitive_diagnostics
                    if self.native.proc.stdin is not None:
                        try:
                            self.native.proc.stdin.write(self.native.reader.encode(self.native.attestation.request))
                            await self.native.proc.stdin.drain()
                        except (BrokenPipeError, ConnectionResetError):
                            pass
                    await self.initialize_rpc()
                    while True:
                        self.skip = False
                        async for event in self.receive_record():
                            yield event
                        if self.finished:
                            break
                        if self.skip:
                            continue
                        if self.awaiting_native_attestation:
                            try:
                                self.native.attestation = self.native.attestation.accept(self.payload)
                            except AttestationError as error:
                                await error.refuse(self)
                                break
                            if self.native.attestation.observed:
                                self.inputs.queue.begin(self.prompt_state)
                                self.ensure_input_forwarding()
                        async for event in self.payload.consume(self):
                            yield event
                        if self.finished:
                            break
                        if self.skip:
                            continue
                        async for event in self.stats.settle(self):
                            yield event
                        if self.finished:
                            break
                    self.finished = False
                    await self.retain_or_close()
                    await self.finish_diagnostics()
                    for event in self.finish_result():
                        yield event
            finally:
                if not self.native_session.child_state.retained:
                    await self.native_session.close()

    async def receive_record(self) -> AsyncIterator[events.AgentEvent]:
        while self.rejected_commands:
            yield self.rejected_commands.pop(0)
        self.rejected_signal.clear()
        try:
            self.line = await self.watchdog.read(self)
        except TurnWaitExpired as expired:
            async for event in expired.wait.expire(self):
                yield event
            return
        if not self.line:
            if self.awaiting_native_attestation:
                self.output.preflight_failure = FailureReason.PREFLIGHT_EXIT
                self.output.diagnostic = {
                    "elapsed_ms": round(
                        (self.loop.time() - self.watchdog.launch_started_at) * 1000
                    ),
                    "spawn_ms": self.watchdog.spawn_ms,
                }
                self.output.diagnostic.update(self.watchdog.session_load.diagnostic)
                self.output.record_failure(
                    failures.InputIdUnavailable(
                        "Pi native input-ID capability preflight ended before attestation."
                    )
                )
            self.finish_request()
            return
        self.line = self.line.strip()
        if not self.line:
            self.skip = True
            return
        try:
            self.payload = PiRpcChannel.decode_record(self.line)
        except json.JSONDecodeError:
            self.skip = True
            return
        except MalformedRecord as error:
            if self.awaiting_native_attestation:
                self.output.record_failure(
                    failures.InputIdUnavailable(
                        f"Invalid Pi capability preflight response: {error}"
                    )
                )
                await self.native.shutdown()
                self.finish_request()
                return
            raise

    def grant_prompt(self) -> None:
        """The permitted write starts sending input and starts its timer."""
        self.prompt_state = self.prompt_state.dispatch()
        self.watchdog.await_input()

    async def invalidate_identity(self) -> tuple[events.AgentEvent, ...]:
        self.native.attestation = self.native.attestation.invalidate()
        self.usage.invalidate()
        self.output.discard_text()
        updates = (self.context_info(), events.TurnState(
            state="failed",
            reason_code="session_identity_uncertain",
            elapsed_ms=0,
            phase=ShutdownPhase(),
        ))
        self.output.record_failure(failures.IdentityUncertain(_IDENTITY_FAILURE_TEXT))
        await self.abort_stalled_rpc()
        self.finish_request()
        return updates

    def request_retried(self) -> None:
        """Pi compacted after an overflow and re-sends the same request."""
        self.output.request_retried()

    def finish_request(self) -> None:
        """The native session alone closes its running request."""
        self.finished = True

    def record_retry(self, result: ProviderRetryResult) -> None:
        """The provider retry outcome decides whether this turn's failure may run again."""
        self.output.retry_result = result

    async def initialize_rpc(self) -> None:
        self.active_session_file = self.launch.session.session_file
        self.watchdog.reading()

    async def retain_or_close(self) -> None:
        self.tool_socket.assert_complete()
        await self.native.finish_input()
        retention = SessionRetention.from_optional(self.native.attestation.identity)
        if self.prompt_state.permits_retention(self):
            self.native.sensitive_diagnostics = self.output.sensitive
            retention.retain(self.native, self.native_session)
        if not self.native_session.child_state.retained:
            outcome = await self.native.shutdown(graceful=True)
            if isinstance(outcome, TimedOutOutcome):
                self.output.record_failure(failures.BackendDidNotExit("agent backend did not exit"))

    async def finish_diagnostics(self) -> None:
        self.error_text = (
            "" if self.native.proc.alive() else await self.native.proc.stderr_tail()
        )
        self.output.startup_error(self.error_text)

    def finish_result(self) -> Iterable[events.AgentEvent]:
        yield from self.output.done(self, self.error_text)


class TurnSession(NativeSession):
    """A native input adds its declared tools and constructs its connected command."""

    prompt_type = UnwrittenPrompt

    def provider_retry_ended(self, event: pi.AutoRetryEnd) -> tuple[events.AgentEvent, ...]:
        updates = event.outcome.apply(event, self)
        self.record_retry(event.outcome.result(event.attempt, self.output.redact))
        return updates

    def __init__(self, launch, make_command: Callable[..., commands.Prompt], *,
                 tool_mode: NativeToolMode, **options):
        super().__init__(launch, **options)
        self.tool_mode = tool_mode
        self.make_command = make_command

    async def open_transport(self, resources: AsyncExitStack) -> UnboundToolTransport:
        unbound = await self.tool_mode.open_transport(self, resources)
        self.command = self.make_command(owner_tools=unbound.listener.connection)
        del self.make_command
        self.output.sensitive = bool(self.command.images)
        return unbound

    async def context_proof(self):
        if not self.started_input or not self.accepts_output:
            raise SelectedToolDenied("Native tool has no started input")

    @handles(pi.ToolExecutionStart)
    async def tool_started(self, event):
        await self.context_proof()
        self.tool_socket.tool_started(event)

    @handles(pi.ToolExecutionEnd)
    async def tool_finished(self, event):
        self.tool_socket.tool_finished(event, self.command.input_id)

    def reject_prompt(self, response):
        error = self.output.error(response.error.text_or("Prompt was rejected"),
                                  source=UnrecordedFailureSource())
        yield self.watchdog.state(
            "failed", "prompt_rejected", 0, phase=ShutdownPhase()
        )
        yield error
        self.finish_request()

    def matches_original_input(self, message):
        return self.inputs.permits_interaction and message.matches_input(
            self.command.message, self.command.input_id
        )

    async def accept_user_message(
        self, message: UserMessage, event: pi.MessageStart
    ) -> tuple[events.AgentEvent, ...]:
        """Own the original/follow-up input effect across all decoded user messages."""
        if self.prompt_state.awaiting_start and self.matches_original_input(message):
            if not self.input_start.original(self.command.input_id, self.command.message):
                self.inputs.uncertain = True
                self.output.record_failure(
                    failures.InputMissing("Pi input start did not match the durable attempt.")
                )
                await self.abort_stalled_rpc()
                self.finish_request()
                return ()
            self.prompt_state = self.prompt_state.start(event)
            self.ensure_input_forwarding()
            return self.input_start.started_events
        if self.started_input:
            matched, input_id = self.inputs.mark_started(self, event)
            if matched:
                return (events.FollowingInputStarted(id=input_id),)
            self.inputs.uncertain = True
            self.output.final_assistant_stop = False
            self.output.record_failure(
                failures.FollowupUnrecognized(
                    "Pi RPC saw an unrecognized follow-up user message start."
                )
            )
        else:
            self.inputs.uncertain = True
            self.output.final_assistant_stop = False
            self.output.record_failure(
                failures.InputMissing("Pi RPC run ended without this prompt's user message start.")
            )
        await self.abort_stalled_rpc()
        self.finish_request()
        return ()
