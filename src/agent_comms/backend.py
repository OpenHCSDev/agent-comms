"""Agent communications — streaming agent backend runner.

Runs the selected native Pi process through its verified RPC launch contract.
Pi's JSON-lines events carry text deltas, tool arguments and tool results for
thinking indicators and tool-call cards.

Events are frozen nominal values declared in :mod:`agent_events`.

Native terminal failures yield ``done`` with ``ok=False``. Unexpected producer
faults retain their original exception for the existing owner-turn failure
publisher after child retirement. Provider failures reported as an assistant
message also fail the turn, carrying ``errorMessage`` and typed diagnostics.
"""

from __future__ import annotations

import asyncio
import getpass
import json
import os
import secrets
import tempfile
import unicodedata
from collections.abc import AsyncGenerator, AsyncIterator, Awaitable, Callable, Iterator, Sequence
from contextlib import AbstractContextManager, AsyncExitStack, aclosing, contextmanager, nullcontext
from functools import partial
from pathlib import Path
from typing import Any, ClassVar

from .child_process import ProcessIdentity
from .request_progress import RequestProgress

from . import agent_events as events
from . import pi_commands as commands
from . import pi_events as pi
from . import turn_failure as failures
from .child_process import TimedOutOutcome
from .diagnostics import FailureReason
from .extension_ui import ExtensionUiSession
from .image_inputs import ImageInput
from .turn_context import InputContributionCoordinates
from .maintenance_barrier import MaintenanceBarrier
from .native_attestation import AttestationError, SavedSessionReopenError
from .native_custody import (
    BorrowedNative,
    EmptyNative,
    NativeCleanupFailed,
    NativeCustody,
    PiSessionChild,
    RetainedNative,
)
from .native_pi import NativePiRpcLaunch, NativePiUnavailable
from .coordinator import Coordination
from .native_session_reopen import NativeSessionIdentity
from .native_startup import NATIVE_STARTUP_POLICY, NativeStartupAdmission
from .pi_rpc import PiRpcChannel
from .selected_source import SessionRevision, SessionRevisionUnavailable
from .selected_tool_broker import SelectedToolDenied
from .turn_admission import UnwrittenPrompt
from .turn_inputs import InputForwarding
from .turn_output import TurnOutput
from .turn_phase import ShutdownPhase, TurnPhase
from .turn_stats import StatsRequest
from .turn_usage import UsageAccount
from .turn_watchdog import ProgressWatchdog


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
MODEL_WAIT_TIMEOUT_SECONDS = 360.0
assert MODEL_WAIT_TIMEOUT_SECONDS > _PI_0_85_1_PROVIDER_IDLE_TIMEOUT_SECONDS
RPC_ABORT_GRACE_SECONDS = 2.0
CAPABILITY_PREFLIGHT_TIMEOUT_SECONDS = NATIVE_STARTUP_POLICY.readiness_seconds
PROMPT_START_TIMEOUT_SECONDS = 180.0
_IDENTITY_FAILURE_TEXT = "Pi session identity changed during this turn."


class PersistentPiSession:
    """Serialize borrowing and retirement of one native session's actual child."""

    def __init__(self) -> None:
        self.lock = asyncio.Lock()
        self.custody: NativeCustody = EmptyNative()

    @property
    def available(self) -> bool:
        return self.custody.available

    async def open(
        self, launch, *, reuse, require_input_id, startup, finish_event, watchdog
    ) -> PiSessionChild:
        key = (launch, launch.configuration.auth_revision())
        child = self.custody.reuse(key) if reuse else None
        reused = child is not None
        if child is None:
            await self.close()
            attestation = await self.custody.expected(launch, require_input_id)
            await startup.acquire(finish_event)
            watchdog.launching(asyncio.get_running_loop().time, launch.session.session_file)
            with startup.measurements.operation("native_spawn"):
                child = await PiSessionChild.start(key, attestation)
            self.custody = BorrowedNative(child, self.custody)
        else:
            watchdog.launching(asyncio.get_running_loop().time, launch.session.session_file)
            self.custody = BorrowedNative(child, EmptyNative())
        watchdog.spawned(reused)
        return child

    def retain(self, child: PiSessionChild, identity: NativeSessionIdentity) -> bool:
        if not child.proc.alive():
            return False
        try:
            revision = SessionRevision.observe(identity.session_file).require_available()
        except SessionRevisionUnavailable:
            return False
        self.custody = RetainedNative(child, identity, revision)
        return True

    async def close(self) -> None:
        self.custody = self.custody.retire()
        try:
            self.custody = await self.custody.closed()
        except NativeCleanupFailed as error:
            self.custody = error.successor
            raise

    async def close_idle(self) -> None:
        async with self.lock:
            await self.close()

    def require_reopen(self, identity: NativeSessionIdentity) -> None:
        self.custody = self.custody.retire(self.custody.reopen(identity))

    async def discard_for_external_write(self, identity: NativeSessionIdentity) -> None:
        async with self.lock:
            self.require_reopen(identity)
            await self.close()



async def terminate_task_process(task: asyncio.Task[Any]) -> None:
    """Terminate the backend subprocess owned by an agent turn task."""
    session = TurnSession.active.get(task)
    if session is not None:
        await session.stop_forwarding()
        await session.native_session.close()
        TurnSession.active.pop(task, None)


@contextmanager
def _maintenance_send_boundary(
    root: Path,
    delegate: Callable[[str | None, str, str], AbstractContextManager[bool | None]] | None,
    public_id: str | None,
    native_id: str,
    text: str,
) -> Iterator[bool | None]:
    """Hold the wire lock through the final native stdin.write.

    The managed ACP callback already holds that lock itself. Other backend
    callers get this outer guard; a custom callback must not reacquire it.
    """
    if delegate is not None and getattr(delegate, "_maintenance_wire_locked", False):
        with delegate(public_id, native_id, text) as allowed:
            yield allowed
        return
    with MaintenanceBarrier(root / "registry.json").admit_ingress():
        with delegate(public_id, native_id, text) if delegate else nullcontext(True) as allowed:
            yield allowed


async def stream_agent_events(
    agent_bin: str,
    agent_args: Sequence[str],
    task: str,
    cwd: str,
    env_extra: dict[str, str] | None = None,
    session_file: str | None = None,
    steering_queue: asyncio.Queue[str | dict[str, Any]] | None = None,
    finish_event: asyncio.Event | None = None,
    images: Sequence[ImageInput] = (),
    model_wait_timeout: float | None = MODEL_WAIT_TIMEOUT_SECONDS,
    rpc_abort_grace: float = RPC_ABORT_GRACE_SECONDS,
    require_input_id: bool = True,
    send_boundary: (
        Callable[[str | None, str, str], AbstractContextManager[bool | None]] | None
    ) = None,
    interrupt_boundary: (
        Callable[[str | None, str, str], AbstractContextManager[bool | None]] | None
    ) = None,
    native_start: Callable[[str | None, str, str], bool] | None = None,
    persistent_session: PersistentPiSession | None = None,
    ui_request: Callable[[pi.DialogUiRequest], Awaitable[pi.ExtensionUiChoice]] | None = None,
    context_contributions: tuple[InputContributionCoordinates, ...] = (),
    request_observer: Callable[[RequestProgress, ProcessIdentity], None] | None = None,
) -> AsyncIterator[events.AgentEvent]:
    """Run the backend; native completion ends with a ``done`` event.

    The no-progress watchdog applies only while waiting for the model. A running
    tool has no deadline in this intentionally incomplete first slice: preventing
    false model-stall kills does not solve a hung tool, which requires a separate
    durable tool-liveness policy. Durable retry decisions belong to the
    coordinator, never this transport adapter.
    """
    owner = asyncio.current_task()
    try:
        async with persistent_session.lock if persistent_session is not None else nullcontext():
            try:
                launch = await Coordination.run_worker(partial(
                    (persistent_session.custody if persistent_session is not None else EmptyNative()).managed_launch,
                    agent_bin,
                    tuple(agent_args),
                    worktree=Path(cwd),
                    environment=env_extra,
                    session_file=session_file,
                ))
            except (OSError, ValueError, NativePiUnavailable) as error:
                yield events.Done(ok=False, reason_code="native_launch_invalid", text=str(error))
                return
            from .session_fence import session_writer_fence

            async with session_writer_fence(launch.session.session_file):
                try:
                    async with aclosing(
                        TurnSession(
                            launch,
                            task,
                            steering_queue=steering_queue,
                            finish_event=finish_event,
                            images=images,
                            context_contributions=context_contributions,
                            model_wait_timeout=model_wait_timeout,
                            rpc_abort_grace=rpc_abort_grace,
                            require_input_id=require_input_id,
                            send_boundary=send_boundary,
                            native_start=native_start,
                            interrupt_boundary=interrupt_boundary,
                            persistent_session=persistent_session,
                            ui_request=ui_request,
                            request_observer=request_observer,
                        ).run()
                    ) as stream:
                        async for event in stream:
                            yield event
                finally:
                    if owner is not None:
                        await terminate_task_process(owner)
    except Exception:
        # The owner-turn publisher owns diagnostic privacy and input settlement.
        # Preserve the producer's original cause instead of fabricating a terminal.
        if owner is not None:
            await terminate_task_process(owner)
        raise


class TurnSession:
    """One Pi turn: owns input admission, native identity, settlement and child lifetime."""

    active: ClassVar[dict[asyncio.Task[Any], TurnSession]] = {}

    def __init__(
        self,
        launch: NativePiRpcLaunch,
        task: str,
        steering_queue: asyncio.Queue[str | dict[str, Any]] | None = None,
        finish_event: asyncio.Event | None = None,
        images: Sequence[ImageInput] = (),
        model_wait_timeout: float | None = MODEL_WAIT_TIMEOUT_SECONDS,
        rpc_abort_grace: float = RPC_ABORT_GRACE_SECONDS,
        require_input_id: bool = True,
        send_boundary: (
            Callable[[str | None, str, str], AbstractContextManager[bool | None]] | None
        ) = None,
        interrupt_boundary: (
            Callable[[str | None, str, str], AbstractContextManager[bool | None]] | None
        ) = None,
        native_start: Callable[[str | None, str, str], bool] | None = None,
        persistent_session: PersistentPiSession | None = None,
        ui_request: Callable[[pi.DialogUiRequest], Awaitable[pi.ExtensionUiChoice]] | None = None,
        startup: NativeStartupAdmission | None = None,
        context_contributions: tuple[InputContributionCoordinates, ...] = (),
        request_observer: Callable[[RequestProgress, ProcessIdentity], None] | None = None,
    ):
        self.launch = launch
        self.task = task
        self.finish_event = finish_event
        self.images = images
        self.context_contributions = context_contributions
        self.request_observer = request_observer
        self.watchdog = ProgressWatchdog(
            model_wait_timeout, PROMPT_START_TIMEOUT_SECONDS, CAPABILITY_PREFLIGHT_TIMEOUT_SECONDS
        )
        self.rpc_abort_grace = rpc_abort_grace
        self.require_input_id = require_input_id
        self.send_boundary = send_boundary
        self.interrupt_boundary = interrupt_boundary
        self.native_start = native_start
        self.persistent_session = persistent_session
        self.native_session = (
            persistent_session if persistent_session is not None else PersistentPiSession()
        )
        self.extension_ui = ExtensionUiSession(ui_request)
        self.startup = startup if startup is not None else NativeStartupAdmission.for_launch(launch)
        self.inputs = InputForwarding(steering_queue)
        self.steering_task: asyncio.Task[None] | None = None
        self.admission = UnwrittenPrompt()
        self.stats = StatsRequest()
        self.usage = UsageAccount()
        self.output = TurnOutput(sensitive=bool(images))
        self.rejected_signal = asyncio.Event()

    @property
    def has_start_listener(self):
        return self.native_start is not None

    def notify_input_started(self, public_id, native_id, text):
        return self.native_start is None or self.native_start(public_id, native_id, text)

    def native_phase_changes(self, previous: TurnPhase) -> Iterator[events.NativePhaseChanged]:
        """Publish the actual observer phase without storing another phase copy."""
        if self.watchdog.phase != previous:
            yield events.NativePhaseChanged(self.watchdog.phase, self.native.proc.identity)

    @property
    def started_input(self):
        return self.admission.started and self.inputs.permits_admission

    @property
    def awaiting_native_attestation(self):
        return self.require_input_id and not self.native.attestation.observed

    async def consume_native_event(self, event):
        """Observe one decoded event through the shared native lifecycle owner."""
        event.observe_request(self.record_request_progress)
        previous = self.watchdog.phase
        async for update in self.watchdog.observe(event, self):
            yield update
        async for update in self.apply_native_event(event):
            yield update
        self.watchdog.transition(event, self.active_tools)
        for update in self.native_phase_changes(previous):
            yield update

    def record_request_progress(self, progress: RequestProgress) -> None:
        """Record the original sample without borrowing global publication locks.

        The acquired native process supplies custody identity; the configured
        observer binds the original public turn lease. No phase owns a copy of
        the transport clock or request lifecycle.
        """
        if self.request_observer is not None:
            self.request_observer(progress, self.native.proc.identity)

    async def apply_native_event(self, event):
        async for update in event.apply(self):
            yield update

    @property
    def accepts_output(self):
        return self.native.attestation.trustworthy

    def matches_original_input(self, message):
        return self.inputs.permits_admission and message.matches_input(
            self.task, self.original_input_id, self.require_input_id
        )

    def committable_message(self, text):
        return self.started_input and self.accepts_output and self.output.matches_message(text)

    def ensure_input_forwarding(self):
        if self.steering_task is None and self.inputs.can_forward(self.native.proc.stdin):
            self.stdin = self.native.proc.stdin
            self.steering_task = asyncio.create_task(self.inputs.forward(self))

    def observe_input_during_abort(self, event):
        if self.accepts_output:
            matched, identity = self.inputs.mark_started(self, event)
            if matched:
                self.started_during_abort.append(identity)

    async def admit_user_message(self, message, event):
        """Own the original/follow-up input effect across all decoded user messages."""
        if self.admission.awaiting_start and self.matches_original_input(message):
            if not self.notify_input_started(None, self.original_input_id, self.task):
                self.inputs.uncertain = True
                self.output.record_failure(
                    failures.InputMissing("Pi input start did not match the durable attempt.")
                )
                await self.abort_stalled_rpc()
                self.finished = True
                return
            self.admission = self.admission.start(event)
            if self.has_start_listener:
                yield events.InputStarted(id=None)
            self.ensure_input_forwarding()
        elif self.started_input:
            matched, input_id = self.inputs.mark_started(self, event)
            if matched:
                yield events.InputStarted(id=input_id)
            else:
                self.inputs.uncertain = True
                self.output.final_assistant_stop = False
                self.output.record_failure(
                    failures.FollowupUnrecognized(
                        "Pi RPC saw an unrecognized follow-up user message start."
                    )
                )
                await self.abort_stalled_rpc()
                self.finished = True
                return
        else:
            self.inputs.uncertain = True
            self.output.final_assistant_stop = False
            self.output.record_failure(
                failures.InputMissing("Pi RPC run ended without this prompt's user message start.")
            )
            await self.abort_stalled_rpc()
            self.finished = True
            return

    def permits_live_receipt(self) -> bool:
        """Only tracked, live input may publish an informational package receipt."""
        if self.stats.requested:
            return False
        return self.require_input_id and self.admission.permits_extension_ui(self)

    async def stop_forwarding(self) -> None:
        if self.steering_task is not None:
            self.steering_task.cancel()
            await asyncio.gather(self.steering_task, return_exceptions=True)

    async def abort_stalled_rpc(self) -> None:
        await self.stop_forwarding()
        if self.native.proc.stdin is not None and self.native.proc.returncode is None:
            abort_deadline = self.loop.time() + max(0.0, self.rpc_abort_grace)
            try:
                self.native.proc.stdin.write(
                    self.native.reader.encode(commands.Abort(id="agent-comms-watchdog"))
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
                    except (ValueError, TypeError, UnicodeError):
                        continue
                    abort_payload.observe_abort(self)
                    if (
                        isinstance(abort_payload, pi.Response)
                        and abort_payload.command is commands.Abort
                    ):
                        break
            except (TimeoutError, OSError):
                pass
        if self.native.proc.returncode is None:
            await self.native.proc.stop()

    def context_info(self) -> events.AgentInfo:
        return events.AgentInfo(
            model=self.model_name,
            session_name=self.session_name,
            session_file=self.active_session_file,
            context_used=self.usage.used,
            context_size=self.usage.size,
        )

    async def open_transport(self, resources: AsyncExitStack) -> None:
        """Leaf-owned launch resources enter the original turn's custody."""

    @contextmanager
    def native_acquisition(self):
        """The original acquisition failures preserve their source disposition."""
        try:
            yield
        except (OSError, TimeoutError, SelectedToolDenied, SavedSessionReopenError) as error:
            failure = NativePiUnavailable(
                f"Native resource acquisition failed: {type(error).__name__}: {error}"
            )
            failure.__cause__ = error
            self.admission.raise_native_failure(failure, self.launch.session.attestation())
        except NativePiUnavailable as error:
            self.admission.raise_native_failure(error, self.launch.session.attestation())

    async def acquire_native(self, resources: AsyncExitStack, *, reuse: bool) -> PiSessionChild:
        """Acquire leaf transport once in the original turn's resource lifetime."""
        resources.callback(self.startup.release)
        with self.native_acquisition():
            with self.startup.measurements.operation("open_transport"):
                await self.open_transport(resources)
            return await self.open_native(reuse=reuse)

    async def open_native(self, *, reuse: bool) -> PiSessionChild:
        """Initial acquisition and prepared continuation share the same custody."""
        with self.startup.measurements.operation("native_open"):
            return await self.native_session.open(
                self.launch, reuse=reuse, require_input_id=self.require_input_id,
                startup=self.startup, finish_event=self.finish_event, watchdog=self.watchdog,
            )

    async def resume_prepared(self, resources: AsyncExitStack) -> None:
        """Borrow the prepared source through the existing transport and turn."""
        with self.native_acquisition():
            self.native = await self.open_native(reuse=True)
        await resources.enter_async_context(self.native.failures())
        resources.callback(self.native.reader.pending.cancel_all)

    async def run(self) -> AsyncGenerator[events.AgentEvent, None]:
        self.finished = self.skip = False
        self.loop = asyncio.get_running_loop()
        self.owner = asyncio.current_task()
        async with AsyncExitStack() as resources:
            try:
                self.native = await self.acquire_native(
                    resources, reuse=self.persistent_session is not None
                )
                if self.owner is not None:
                    self.active[self.owner] = self
                async with self.native.failures():
                    self.prepare_launch()
                    self.output.sensitive |= self.native.sensitive_diagnostics
                    if self.native.proc.stdin is not None:
                        try:
                            if not self.require_input_id:
                                self.grant_prompt()
                            self.native.proc.stdin.write(self.stdin_payload)
                            await self.native.proc.stdin.drain()
                        except (BrokenPipeError, ConnectionResetError):
                            pass
                    async for event in self.initialize_rpc():
                        yield event
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
                            self.startup.release()
                            await self.input_ready()
                            if self.finished:
                                break
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
                    async for event in self.retain_or_close():
                        yield event
                    async for event in self.finish_diagnostics():
                        yield event
                    async for event in self.finish_result():
                        yield event
            finally:
                try:
                    await self.stop_forwarding()
                    if not self.native_session.custody.retained:
                        await self.native_session.close()
                finally:
                    self.active.pop(self.owner, None)

    async def receive_record(self) -> AsyncIterator[events.AgentEvent]:
        while self.rejected_commands:
            yield self.rejected_commands.pop(0)
        self.rejected_signal.clear()
        try:
            self.line = await self.watchdog.read(self)
        except TimeoutError:
            async for event in self.watchdog.expire(self):
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
                if self.watchdog.session_bytes is not None:
                    self.output.diagnostic["session_bytes"] = self.watchdog.session_bytes
                self.output.record_failure(
                    failures.InputIdUnavailable(
                        "Pi native input-ID capability preflight ended before attestation."
                    )
                )
            self.finished = True
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
        except (ValueError, TypeError) as error:
            if self.awaiting_native_attestation:
                self.output.record_failure(
                    failures.InputIdUnavailable(
                        f"Invalid Pi capability preflight response: {error}"
                    )
                )
                await self.native.proc.stop()
                self.finished = True
                return
            raise

    def grant_prompt(self) -> None:
        """Original granted write starts both admission and its input-start clock."""
        self.admission = self.admission.dispatch()
        self.watchdog.await_input()

    async def input_ready(self) -> None:
        assert self.native.proc.stdin is not None
        try:
            self.boundary_context = _maintenance_send_boundary(
                Path(
                    self.launch.env.get("AGENT_COMMS_ROOT")
                    or os.environ.get("AGENT_COMMS_ROOT")
                    or str(Path(tempfile.gettempdir()) / f"agent-comms-startup-{getpass.getuser()}")
                ),
                self.send_boundary,
                None,
                self.original_input_id,
                self.task,
            )
            with self.boundary_context as self.authorized:
                if self.authorized:
                    self.grant_prompt()
                    self.native.proc.stdin.write(self.prompt_payload)
            if not self.authorized:
                self.output.record_failure(
                    failures.InputMissing("Input authority changed before Pi prompt send.")
                )
                await self.native.proc.stop()
                self.finished = True
                return
            await self.native.proc.stdin.drain()
        except (BrokenPipeError, ConnectionResetError):
            self.output.record_failure(
                failures.InputIdUnavailable(
                    "Pi RPC prompt could not be sent after capability preflight."
                )
            )
            await self.native.proc.stop()
            self.finished = True
            return

    async def invalidate_identity(self) -> AsyncIterator[events.AgentEvent]:
        self.native.attestation = self.native.attestation.invalidate()
        self.usage.invalidate()
        self.output.discard_text()
        yield self.context_info()
        yield events.TurnState(
            state="failed",
            reason_code="session_identity_uncertain",
            elapsed_ms=0,
            phase=ShutdownPhase(),
        )
        self.output.record_failure(failures.IdentityUncertain(_IDENTITY_FAILURE_TEXT))
        await self.abort_stalled_rpc()
        self.finished = True

    def prepare_launch(self) -> None:
        self.prompt_id = (
            f"agent-comms-prompt-{secrets.token_hex(16)}"
            if self.persistent_session is not None
            else "agent-comms-prompt"
        )
        self.original_input_id = secrets.token_hex(16)
        self.prompt_request = commands.Prompt(
            id=self.prompt_id,
            input_id=self.original_input_id,
            message=self.task,
            images=self.images or None,
            context_contributions=self.context_contributions,
        )
        self.prompt_payload = PiRpcChannel.command_bytes(self.prompt_request)
        self.stdin_payload = PiRpcChannel.command_bytes(self.native.attestation.request)
        if not self.require_input_id:
            self.stdin_payload += self.prompt_payload

    async def initialize_rpc(self) -> AsyncIterator[events.AgentEvent]:
        self.explicit_interrupt = False
        self.rejected_commands: list[events.AgentEvent] = []
        if self.inputs.can_forward(self.native.proc.stdin):
            self.stdin = self.native.proc.stdin
            if not self.require_input_id:
                self.ensure_input_forwarding()
        self.model_name: str | None = None
        self.session_name: str | None = None
        self.active_session_file = self.launch.session.session_file
        self.settlement_count = 0
        self.native.reader.pending.cancel_all()
        self.native.reader.pending.add(
            commands.GetState,
            self.native.attestation.request.id,
            request=self.native.attestation.request,
        )
        self.native.reader.pending.add(
            commands.Prompt,
            self.prompt_id,
            request=self.prompt_request,
        )
        self.watchdog.reading()
        self.active_tools: set[str] = set()
        self.started_during_abort: list[str | None] = []
        if False:
            yield

    async def retain_or_close(self) -> AsyncIterator[events.AgentEvent]:
        await self.stop_forwarding()
        self.native.reader.pending.cancel_all()
        identity = self.native.attestation.identity
        if (
            self.persistent_session is not None
            and self.require_input_id
            and identity is not None
            and self.admission.permits_retention(self)
        ):
            self.native.sensitive_diagnostics = self.output.sensitive
            self.native_session.retain(self.native, identity)
        if not self.native_session.custody.retained:
            outcome = await self.native.proc.finish()
            if isinstance(outcome, TimedOutOutcome):
                self.output.record_failure(failures.BackendDidNotExit("agent backend did not exit"))
        if False:
            yield

    async def finish_diagnostics(self) -> AsyncIterator[events.AgentEvent]:
        self.error_text = (
            "" if self.native_session.custody.retained else await self.native.stderr_task
        )
        self.output.startup_error(self.error_text)
        if False:
            yield

    async def finish_result(self) -> AsyncIterator[events.AgentEvent]:
        yield self.output.done(self, self.error_text)
