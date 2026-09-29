"""Agent communications — streaming agent backend runner.

Runs the selected native Pi process through its verified RPC launch contract.
Pi's JSON-lines events carry text deltas, tool arguments and tool results for
thinking indicators and tool-call cards.

Events are frozen nominal values declared in :mod:`agent_events`.

The runner never raises on backend failure; it yields ``done`` with
``ok=False`` and the error as text. Callers own presentation. Provider
failures that Pi reports as a completed assistant message (for example an
exhausted usage limit) also fail the turn, carrying ``errorMessage``.
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
from contextlib import AbstractContextManager, aclosing, contextmanager, nullcontext, suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import agent_events as events
from . import pi_commands as commands
from . import pi_events as pi
from . import pi_payloads
from . import turn_failure as failures
from . import turn_phase as phases
from .child_process import AttachedChild, BoundedRun, TimedOutOutcome
from .diagnostics import FailureReason
from .field_codec import FieldCodec
from .image_inputs import ImageInput
from .maintenance_barrier import MaintenanceBarrier
from .native_pi import CAPABILITY as NATIVE_INPUT_CAPABILITY
from .native_pi import NativePiRpcLaunch, NativePiUnavailable
from .native_startup import NATIVE_STARTUP_POLICY, NativeStartupAdmission
from .pi_rpc import PiRpcChannel
from .store_files import _store_lock
from .turn_inputs import InputForwarding
from .turn_stats import StatsRequest
from .turn_usage import UsageAccount


def compaction_summary(value: Any) -> str:
    """Preserve saved Markdown, excluding terminal control codes."""
    if not isinstance(value, str):
        return ""
    return "".join(
        char if char in "\n\t" or not unicodedata.category(char).startswith("C") else " "
        for char in value.replace("\r\n", "\n")
    )


_TOOL_KINDS = {
    "bash": "execute",
    "read": "read",
    "write": "edit",
    "edit": "edit",
    "grep": "search",
    "glob": "search",
}
_ACTIVE_PROCESSES: dict[asyncio.Task[Any], AttachedChild] = {}
_ACTIVE_STDERR_TASKS: dict[asyncio.Task[Any], asyncio.Task[str]] = {}
_ACTIVE_STEERING: dict[asyncio.Task[Any], asyncio.Task[None]] = {}
_ACTIVE_INPUT_RESTORERS: dict[asyncio.Task[Any], Callable[[], None]] = {}
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


def _unique_json_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate live-status field")
        result[key] = value
    return result


def _pi_mcp_live_receipt(
    payload: pi.ExtensionUiRequest, input_id: str
) -> pi_payloads.McpLiveReceipt | None:
    """Project only a bounded package claim from the same Pi child and native input.

    This is observed live status, never MCP approval or call authorization. A
    same-user Pi extension may mimic an extension UI status; this is not a
    cryptographic attestation of the package against other local extensions.
    """
    if payload.method != "setStatus" or payload.status_key != "pi-mcp/live-v1":
        return None
    text = payload.status_text
    if text is None or len(text) > 8192:
        return None
    try:
        data = json.loads(text, object_pairs_hook=_unique_json_pairs)
    except (ValueError, TypeError):
        return None
    try:
        receipt = FieldCodec.decode(pi_payloads.McpLiveReceipt, data)
    except (TypeError, ValueError):
        return None
    return receipt if receipt.input_id == input_id else None


_FileRevision = tuple[int, int, int, int, int]


def _file_revision(path: Path) -> _FileRevision | None:
    try:
        stat = path.stat()
    except OSError:
        return None
    return stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns


def _session_revision(
    session_file: str | None,
) -> tuple[_FileRevision, _FileRevision | None] | None:
    if not isinstance(session_file, str) or not session_file:
        return None
    session = _file_revision(Path(session_file))
    if session is None:
        return None
    return session, _file_revision(Path(session_file + ".input-proof"))


class PersistentPiSession:
    """One idle Pi RPC child, owned by one ACP session in one owner process.

    A turn borrows the child only under the saved-session writer fence. The
    revision check detects a separate writer between turns, so its in-memory
    history cannot silently omit a compacted or appended saved session.
    """

    def __init__(self) -> None:
        self.lock = asyncio.Lock()
        self.proc: AttachedChild | None = None
        self.reader: PiRpcChannel | None = None
        self.stderr_task: asyncio.Task[str] | None = None
        self.launch_key: tuple[NativePiRpcLaunch, tuple[int, int]] | None = None
        self.session_file: str | None = None
        self.session_id: str | None = None
        self.revision: tuple[_FileRevision, _FileRevision | None] | None = None
        self.sensitive_diagnostics = False
        self.reopen_required: str | None = None
        self.reopen_session_id: str | None = None
        # A cancellation cannot lose the sole handle to a child still being
        # reaped. Every later borrower waits for this task before launching.
        self._close_task: asyncio.Task[None] | None = None

    def reusable(
        self, launch_key: tuple[NativePiRpcLaunch, tuple[int, int]], session_file: str | None
    ) -> bool:
        return (
            self.reopen_required is None
            and self._close_task is None
            and self.proc is not None
            and self.proc.returncode is None
            and self.reader is not None
            and self.stderr_task is not None
            and self.launch_key == launch_key
            and self.session_file == session_file
            and self.revision is not None
            and self.revision == _session_revision(session_file)
        )

    async def close(self) -> None:
        """Reap the exact child even if a caller is cancelled mid-retirement.

        The lock serializes normal borrowers. A cancelled borrower releases it,
        but the retained cleanup task owns the old process and the next borrow
        must await that same task before opening another Pi child.
        """
        if self._close_task is None:
            proc, stderr_task = self.proc, self.stderr_task

            async def finish() -> None:
                if proc is not None:
                    await proc.stop()
                if stderr_task is not None:
                    await asyncio.gather(stderr_task, return_exceptions=True)

            # Create the independent cleanup BEFORE forgetting the process.
            self._close_task = asyncio.create_task(finish())
            self.proc = None
            self.reader = None
            self.stderr_task = None
            self.launch_key = None
            self.session_file = None
            self.session_id = None
            self.revision = None
            self.sensitive_diagnostics = False
        await asyncio.shield(self._close_task)
        self._close_task = None

    async def close_idle(self) -> None:
        """Wait for a borrowed turn's stats/cleanup before closing its child."""
        async with self.lock:
            await self.close()

    async def discard_for_external_write(self, session_file: str) -> None:
        """Retire the injected in-memory manager; require strict disk validation.

        The old child must die BEFORE another process can rewrite its session.
        A public field assignment or fresh attempt cannot revive that child.
        """
        async with self.lock:
            if self.session_file is not None and self.session_file != session_file:
                raise ValueError("Idle manager belongs to a different saved session")
            expected = self.session_id if self.session_file == session_file else None
            # Poison before the first cancellable await. An interrupted retire
            # cannot make old in-memory history reusable or waive validation.
            self.reopen_required = session_file
            if expected is not None:
                self.reopen_session_id = expected
            await self.close()


@dataclass(frozen=True, slots=True)
class Model:
    id: str
    name: str
    description: str | None = None


def auth_revision() -> tuple[int, int]:
    """Detect credential changes without reading or exposing their contents."""
    root = Path(os.environ.get("PI_CODING_AGENT_DIR", "~/.pi/agent")).expanduser()
    try:
        stat = (root / "auth.json").stat()
        return stat.st_mtime_ns, stat.st_size
    except OSError:
        return 0, 0


async def terminate_task_process(task: asyncio.Task[Any]) -> None:
    """Terminate the backend subprocess owned by an agent turn task."""
    if steering := _ACTIVE_STEERING.pop(task, None):
        steering.cancel()
        await asyncio.gather(steering, return_exceptions=True)
    if restore_inputs := _ACTIVE_INPUT_RESTORERS.pop(task, None):
        restore_inputs()
    proc = _ACTIVE_PROCESSES.pop(task, None)
    if proc is not None:
        await proc.stop()
    stderr_task = _ACTIVE_STDERR_TASKS.pop(task, None)
    if stderr_task is not None:
        if not stderr_task.done():
            stderr_task.cancel()
        await asyncio.gather(stderr_task, return_exceptions=True)


def configured_model(args: Sequence[str]) -> str | None:
    """Return the provider-qualified model selected by backend arguments."""
    provider: str | None = None
    model: str | None = None
    for index, argument in enumerate(args):
        if argument == "--provider" and index + 1 < len(args):
            provider = args[index + 1]
        elif argument.startswith("--provider="):
            provider = argument.partition("=")[2]
        elif argument == "--model" and index + 1 < len(args):
            model = args[index + 1]
        elif argument.startswith("--model="):
            model = argument.partition("=")[2]
    if model is None:
        return None
    if provider is None or model.startswith(f"{provider}/"):
        return model
    return f"{provider}/{model}"


def configured_thinking_level(args: Sequence[str]) -> str | None:
    """Return a thinking level supplied through Pi's CLI arguments."""
    for index, argument in enumerate(args):
        if argument == "--thinking" and index + 1 < len(args):
            return args[index + 1]
        if argument.startswith("--thinking="):
            return argument.partition("=")[2]
    return None


def args_for_model(args: Sequence[str], model: str | None) -> list[str]:
    """Replace Pi's provider/model arguments with one persisted selection."""
    if model is None or "/" not in model:
        return list(args)
    provider, model_id = model.split("/", 1)
    result: list[str] = []
    skip = False
    for argument in args:
        if skip:
            skip = False
            continue
        if argument in {"--provider", "--model"}:
            skip = True
            continue
        if argument.startswith(("--provider=", "--model=")):
            continue
        result.append(argument)
    return [*result, "--provider", provider, "--model", model_id]


def args_for_thinking_level(args: Sequence[str], level: str | None) -> list[str]:
    """Replace Pi's thinking argument with one persisted selection."""
    result: list[str] = []
    skip = False
    for argument in args:
        if skip:
            skip = False
            continue
        if argument == "--thinking":
            skip = True
            continue
        if argument.startswith("--thinking="):
            continue
        result.append(argument)
    return [*result, "--thinking", level] if level else result


async def discover_thinking_levels(
    agent_bin: str, agent_args: Sequence[str], model: str | None
) -> list[str]:
    """Ask Pi for the thinking levels supported by one selected model."""
    if os.environ.get("AGENT_COMMS_AGENT_MODELS"):
        return ["off", "minimal", "low", "medium", "high"]
    args = args_for_model(agent_args, model)
    levels: list[str] = []
    try:
        launch = await asyncio.to_thread(
            NativePiRpcLaunch.managed,
            agent_bin,
            tuple([*args, "--no-extensions", "--no-skills", "--no-context-files", "--no-session"]),
            worktree=Path.cwd(),
        )
        async with BoundedRun.session(
            launch.argv, cwd=launch.cwd, env=launch.env, timeout=10, limit=1024 * 1024
        ) as proc:
            assert proc.stdin is not None and proc.stdout is not None
            reader = PiRpcChannel(proc.stdout)
            proc.stdin.write(reader.encode(commands.GetAvailableThinkingLevels(id="thinking")))
            await proc.stdin.drain()
            while line := await reader.readline():
                payload = PiRpcChannel.decode_record(line)
                if (
                    not isinstance(payload, pi.Response)
                    or payload.id != "thinking"
                    or payload.command is not commands.GetAvailableThinkingLevels
                ):
                    continue
                levels = (
                    list(payload.data.levels)
                    if payload.success and payload.data is not None
                    else []
                )
                break
    except (TimeoutError, ValueError, OSError, NativePiUnavailable):
        pass
    return levels or ["off"]


async def discover_models(
    agent_bin: str, agent_args: Sequence[str], selected: str | None = None
) -> list[Model]:
    """Ask a Pi backend for its configured model catalog."""
    explicit = [
        value.strip()
        for value in os.environ.get("AGENT_COMMS_AGENT_MODELS", "").split(",")
        if value.strip()
    ]
    if explicit:
        values = explicit
    else:
        values = []
        try:
            launch = await asyncio.to_thread(
                NativePiRpcLaunch.managed,
                agent_bin,
                tuple(
                    [
                        *agent_args,
                        "--no-extensions",
                        "--no-skills",
                        "--no-context-files",
                        "--no-session",
                    ]
                ),
                worktree=Path.cwd(),
            )
            async with BoundedRun.session(
                launch.argv, cwd=launch.cwd, env=launch.env, timeout=10, limit=8 * 1024 * 1024
            ) as proc:
                assert proc.stdin is not None and proc.stdout is not None
                reader = PiRpcChannel(proc.stdout)
                proc.stdin.write(reader.encode(commands.GetAvailableModels(id="models")))
                await proc.stdin.drain()
                while line := await reader.readline():
                    payload = PiRpcChannel.decode_record(line)
                    if (
                        not isinstance(payload, pi.Response)
                        or payload.id != "models"
                        or payload.command is not commands.GetAvailableModels
                    ):
                        continue
                    for item in payload.data.models if payload.success and payload.data else ():
                        if item.provider and item.id:
                            values.append(f"{item.provider}/{item.id}")
                    break
        except (TimeoutError, ValueError, OSError, NativePiUnavailable):
            pass
    if selected and selected not in values:
        values.insert(0, selected)
    return [Model(value, value) for value in dict.fromkeys(values)]


def tool_kind(name: str) -> str:
    return _TOOL_KINDS.get(name.lower(), "other")


def _short_args(raw: Any, limit: int = 80) -> str:
    try:
        text = json.dumps(raw) if not isinstance(raw, str) else raw
    except (TypeError, ValueError):
        text = str(raw)
    text = text.strip()
    return text[:limit] + ("…" if len(text) > limit else "")


def _tool_title(name: str, args: Any) -> str:
    """Build a concise activity label from structured tool arguments."""
    values = args if isinstance(args, dict) else {}
    if name == "bash":
        detail = values.get("command")
        action = "Run"
    elif name in {"read", "write", "edit"}:
        detail = values.get("path") or values.get("file_path")
        action = name.title()
    elif name == "grep":
        pattern = values.get("pattern")
        path = values.get("path")
        detail = f"{pattern} in {path}" if pattern and path else pattern or path
        action = "Search"
    elif name == "glob":
        detail = values.get("pattern") or values.get("path")
        action = "Find"
    else:
        detail = None
        action = name.replace("_", " ").title()
    return f"{action} {_short_args(detail, 120)}" if detail else action


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
    with _store_lock(root / "wire"):
        MaintenanceBarrier(root / "registry.json").assert_open_unlocked()
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
    fork_session: bool = False,
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
    ui_request: Callable[[pi.ExtensionUiRequest], Awaitable[pi.ExtensionUiChoice]] | None = None,
) -> AsyncIterator[events.AgentEvent]:
    """Run the backend and yield events. Always ends with a ``done`` event.

    The no-progress watchdog applies only while waiting for the model. A running
    tool has no deadline in this intentionally incomplete first slice: preventing
    false model-stall kills does not solve a hung tool, which requires a separate
    durable tool-liveness policy. Durable retry decisions belong to the
    coordinator, never this transport adapter.
    """
    owner = asyncio.current_task()
    terminal_seen = False
    startup = NativeStartupAdmission(
        Path(
            (env_extra or {}).get("AGENT_COMMS_ROOT")
            or os.environ.get("AGENT_COMMS_ROOT")
            or str(Path(tempfile.gettempdir()) / f"agent-comms-startup-{getpass.getuser()}")
        ).expanduser()
    )
    try:
        try:
            launch = await asyncio.to_thread(
                NativePiRpcLaunch.managed,
                agent_bin,
                tuple(agent_args),
                worktree=Path(cwd),
                environment=env_extra,
                session_file=session_file,
                fork_session=fork_session,
            )
        except (OSError, ValueError, NativePiUnavailable) as error:
            yield events.Done(ok=False, reason_code="native_launch_invalid", text=str(error))
            return
        from .session_fence import session_writer_fence

        async with (
            session_writer_fence(session_file),
            persistent_session.lock if persistent_session is not None else nullcontext(),
        ):
            try:
                async with aclosing(
                    TurnSession(
                        launch,
                        task,
                        session_file=session_file,
                        steering_queue=steering_queue,
                        finish_event=finish_event,
                        fork_session=fork_session,
                        images=images,
                        model_wait_timeout=model_wait_timeout,
                        rpc_abort_grace=rpc_abort_grace,
                        require_input_id=require_input_id,
                        send_boundary=send_boundary,
                        native_start=native_start,
                        interrupt_boundary=interrupt_boundary,
                        persistent_session=persistent_session,
                        ui_request=ui_request,
                        startup=startup,
                    ).run()
                ) as stream:
                    async for event in stream:
                        if isinstance(event, events.Done):
                            terminal_seen = True
                        yield event
            finally:
                startup.release()
                if owner is not None:
                    await terminate_task_process(owner)
    except Exception:
        # A malformed RPC row cannot certify a completed turn. Preserve no
        # raw payload/stderr in the wire response and always reap the child.
        if owner is not None:
            await terminate_task_process(owner)
        if not terminal_seen:
            yield events.Done(
                ok=False,
                reason_code="pi_invalid_rpc_event",
                text="Pi RPC returned an invalid event; this turn was not completed.",
            )


class TurnSession:
    """One Pi turn: owns input admission, native identity, settlement and child lifetime."""

    def __init__(
        self,
        launch: NativePiRpcLaunch,
        task: str,
        session_file: str | None = None,
        steering_queue: asyncio.Queue[str | dict[str, Any]] | None = None,
        finish_event: asyncio.Event | None = None,
        fork_session: bool = False,
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
        ui_request: (
            Callable[[pi.ExtensionUiRequest], Awaitable[pi.ExtensionUiChoice]] | None
        ) = None,
        startup: NativeStartupAdmission | None = None,
    ):
        self.launch = launch
        self.task = task
        self.cwd = str(launch.cwd)
        self.session_file = session_file
        self.steering_queue = steering_queue
        self.finish_event = finish_event
        self.fork_session = fork_session
        self.images = images
        self.model_wait_timeout = model_wait_timeout
        self.rpc_abort_grace = rpc_abort_grace
        self.require_input_id = require_input_id
        self.send_boundary = send_boundary
        self.interrupt_boundary = interrupt_boundary
        self.native_start = native_start
        self.persistent_session = persistent_session
        self.ui_request = ui_request
        self.startup = startup
        self.inputs = InputForwarding()
        self.stats = StatsRequest()
        self.usage = UsageAccount()

    async def stderr_tail(self) -> str:
        tail = b""
        assert self.proc.stderr is not None
        while chunk := (await self.proc.stderr.read(4096)):
            tail = (tail + chunk)[-16000:]
        return tail.decode(errors="replace").strip()

    async def read_rpc_line(self, timeout: float | None) -> bytes:
        if self.rejected_signal.is_set():
            return b"\n"
        read_task = asyncio.create_task(self.reader.readline())
        reject_task = asyncio.create_task(self.rejected_signal.wait())
        finish_task = (
            asyncio.create_task(self.finish_event.wait())
            if self.finish_event is not None and (not self.stats.requested)
            else None
        )
        tasks = {read_task, reject_task}
        if finish_task is not None:
            tasks.add(finish_task)
        try:
            done, _ = await asyncio.wait(
                tasks, timeout=timeout, return_when=asyncio.FIRST_COMPLETED
            )
            if not done:
                raise TimeoutError
            if read_task in done:
                return read_task.result()
            if finish_task is not None and finish_task in done:
                read_task.cancel()
                await asyncio.gather(read_task, return_exceptions=True)
                if self.require_input_id and (not self.native_capability_confirmed):
                    return b""
                await self.stats.request(self)
            return b"\n"
        finally:
            for pending in tasks:
                if not pending.done():
                    pending.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    async def abort_stalled_rpc(self) -> None:
        if self.steering_task is not None:
            self.steering_task.cancel()
            await asyncio.gather(self.steering_task, return_exceptions=True)
        if self.proc.stdin is not None and self.proc.returncode is None:
            abort_deadline = self.loop.time() + max(0.0, self.rpc_abort_grace)
            try:
                self.proc.stdin.write(self.reader.encode(commands.Abort(id="agent-comms-watchdog")))
                remaining = max(0.0, abort_deadline - self.loop.time())
                await asyncio.wait_for(self.proc.stdin.drain(), timeout=remaining)
                while remaining := max(0.0, abort_deadline - self.loop.time()):
                    response = await asyncio.wait_for(self.reader.readline(), timeout=remaining)
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
        if self.proc.returncode is None:
            await self.proc.stop()

    def turn_state(
        self,
        state: str,
        reason_code: str,
        elapsed_ms: int,
        *,
        event_phase: str | None = None,
        attempt: tuple[int | None, int | None] | None = None,
    ) -> events.TurnState:
        replay_safe = not (
            self.prompt_dispatched
            or self.tool_ever_started
            or self.output_started
            or self.inputs.started
            or self.compaction_started
        )
        return events.TurnState(
            state=state,
            reason_code=reason_code,
            elapsed_ms=max(0, elapsed_ms),
            phase=event_phase or self.phase.declared_name,
            retryable=replay_safe,
            replay_safe=replay_safe,
            side_effects_possible=self.prompt_dispatched
            or self.tool_ever_started
            or self.inputs.started
            or self.compaction_started,
            attempt={"current": attempt[0], "max": attempt[1]} if attempt is not None else None,
        )

    def context_info(self) -> events.AgentInfo:
        return events.AgentInfo(
            model=self.model_name,
            session_name=self.session_name,
            session_file=self.active_session_file,
            context_used=self.usage.used,
            context_size=self.usage.size,
        )

    async def run(self) -> AsyncGenerator[events.AgentEvent, None]:
        self.finished = self.skip = False
        self.prepare_launch()
        if self.finished:
            return
        async for event in self.validate_reopen():
            yield event
        if self.finished:
            return
        async for event in self.spawn_child():
            yield event
        if self.finished:
            return
        self.initialize_output()
        if self.finished:
            return
        async for event in self.initialize_rpc():
            yield event
        if self.finished:
            return
        while True:
            self.skip = False
            async for event in self.receive_record():
                yield event
            if self.finished:
                break
            if self.skip:
                continue
            await self.attest_input()
            if self.finished:
                break
            if self.skip:
                continue
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
            if self.skip:
                continue
        self.finished = False
        async for event in self.retain_or_close():
            yield event
        if self.finished:
            return
        async for event in self.finish_diagnostics():
            yield event
        if self.finished:
            return
        async for event in self.finish_result():
            yield event
        if self.finished:
            return

    def record_failure(self, failure: failures.TurnFailure) -> None:
        if failure.supersedes(self.failure):
            self.failure = failure

    @property
    def fail_reason(self) -> str:
        return self.failure.text if self.failure else ""

    async def receive_record(self) -> AsyncIterator[events.AgentEvent]:
        while self.rejected_commands:
            yield self.rejected_commands.pop(0)
        self.rejected_signal.clear()
        try:
            if self.stats.requested:
                self.read_timeout: float | None = 5.0
            elif self.require_input_id and (not self.native_capability_confirmed):
                self.read_timeout = max(0.0, self.preflight_deadline - self.loop.time())
            elif self.active_tools or self.model_wait_timeout is None:
                self.read_timeout = None
            else:
                self.read_timeout = max(
                    0.0, self.last_model_progress + self.model_wait_timeout - self.loop.time()
                )
            if (
                self.prompt_start_deadline is not None
                and (not self.initial_input_started)
                and (not self.phase.pauses_input_clock)
            ):
                self.start_wait = max(0.0, self.prompt_start_deadline - self.loop.time())
                self.read_timeout = (
                    min(self.read_timeout, self.start_wait)
                    if self.read_timeout is not None
                    else self.start_wait
                )
            self.line = await self.read_rpc_line(self.read_timeout)
        except TimeoutError:
            async for event in self.handle_timeout():
                yield event
            return
        if not self.line:
            if self.require_input_id and (not self.native_capability_confirmed):
                self.preflight_failure = FailureReason.PREFLIGHT_EXIT
                self.diagnostic = {
                    "elapsed_ms": round((self.loop.time() - self.launch_started_at) * 1000),
                    "spawn_ms": self.spawn_ms,
                }
                if self.session_bytes is not None:
                    self.diagnostic["session_bytes"] = self.session_bytes
                self.record_failure(
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
            if self.require_input_id and not self.native_capability_confirmed:
                self.record_failure(
                    failures.InputIdUnavailable(
                        f"Invalid Pi capability preflight response: {error}"
                    )
                )
                await self.proc.stop()
                self.finished = True
                return
            raise

    async def attest_input(self) -> None:
        if self.require_input_id and (not self.native_capability_confirmed):
            if (
                not isinstance(self.payload, pi.Response)
                or self.payload.command is not commands.GetState
            ):
                self.record_failure(
                    failures.InputIdUnavailable(
                        "Pi native input-ID capability preflight returned another event."
                    )
                )
                await self.proc.stop()
                self.finished = True
                return
            self.state = self.payload.data
            if (
                self.payload.id != self.preflight_id
                or self.payload.success is not True
                or (self.state is None)
                or (self.state.native_input_proof_capability != NATIVE_INPUT_CAPABILITY)
            ):
                self.record_failure(
                    failures.InputIdUnavailable("Pi native input-ID capability preflight failed.")
                )
                await self.proc.stop()
                self.finished = True
                return
            if (
                self.reused
                and self.persistent_session is not None
                and (
                    self.state.session_id != self.persistent_session.session_id
                    or self.state.session_file != self.persistent_session.session_file
                )
                or (
                    self.validated_session_id is not None
                    and (
                        self.state.session_id != self.validated_session_id
                        or self.state.session_file != self.session_file
                    )
                )
            ):
                self.session_identity_uncertain = True
                self.record_failure(failures.IdentityUncertain(_IDENTITY_FAILURE_TEXT))
                await self.proc.stop()
                self.finished = True
                return
            self.native_capability_confirmed = True
            if self.startup is not None:
                self.startup.release()
            await self.input_ready()

    async def input_ready(self) -> None:
        self.prompt_start_deadline = self.loop.time() + PROMPT_START_TIMEOUT_SECONDS
        assert self.proc.stdin is not None
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
                    self.prompt_dispatched = True
                    self.proc.stdin.write(self.prompt_payload)
            if not self.authorized:
                self.record_failure(
                    failures.InputMissing("Input authority changed before Pi prompt send.")
                )
                await self.proc.stop()
                self.finished = True
                return
            await self.proc.stdin.drain()
        except (BrokenPipeError, ConnectionResetError):
            self.record_failure(
                failures.InputIdUnavailable(
                    "Pi RPC prompt could not be sent after capability preflight."
                )
            )
            await self.proc.stop()
            self.finished = True
            return

    async def invalidate_identity(self) -> AsyncIterator[events.AgentEvent]:
        self.session_identity_uncertain = True
        self.usage.invalidate()
        self.text_parts.clear()
        yield self.context_info()
        yield events.TurnState(
            state="failed",
            reason_code="session_identity_uncertain",
            elapsed_ms=0,
            phase="shutdown",
            retryable=False,
            replay_safe=False,
            side_effects_possible=True,
        )
        self.record_failure(failures.IdentityUncertain(_IDENTITY_FAILURE_TEXT))
        await self.abort_stalled_rpc()
        self.finished = True

    def prepare_launch(self) -> None:
        self.prompt_id = (
            f"agent-comms-prompt-{secrets.token_hex(16)}"
            if self.persistent_session is not None
            else "agent-comms-prompt"
        )
        self.preflight_id = f"agent-comms-preflight-{secrets.token_hex(16)}"
        self.original_input_id = secrets.token_hex(16)
        self.prompt_payload = b""
        self.prompt_payload = PiRpcChannel.command_bytes(
            commands.Prompt(
                id=self.prompt_id,
                input_id=self.original_input_id,
                message=self.task,
                images=self.images or None,
            )
        )
        self.stdin_payload = PiRpcChannel.command_bytes(commands.GetState(id=self.preflight_id))
        if not self.require_input_id:
            self.stdin_payload += self.prompt_payload
        self.launch_key = (self.launch, auth_revision())

    async def validate_reopen(self) -> AsyncIterator[events.AgentEvent]:
        self.reused = False
        if self.persistent_session is not None:
            self.reused = (not self.fork_session) and self.persistent_session.reusable(
                self.launch_key, self.session_file
            )
            if not self.reused:
                await self.persistent_session.close()
        self.loop = asyncio.get_running_loop()
        self.validated_session_id: str | None = None
        if (
            self.persistent_session is not None
            and self.persistent_session.reopen_required is not None
        ):
            if (
                self.session_file != self.persistent_session.reopen_required
                or not self.require_input_id
            ):
                yield events.Done(
                    text="Saved native session requires explicit validated reopen.",
                    ok=False,
                    reason_code="compaction_reopen_invalid",
                )
                self.finished = True
                return
            try:
                from .native_session_reopen import validate_native_reopen

                self.validated_session_id = await asyncio.to_thread(
                    validate_native_reopen,
                    self.launch.package,
                    self.session_file,
                    expected_session_id=self.persistent_session.reopen_session_id,
                )
            except ValueError:
                yield events.Done(
                    text="Saved native session failed strict reopen validation.",
                    ok=False,
                    reason_code="compaction_reopen_invalid",
                )
                self.finished = True
                return
        if not self.reused and self.require_input_id and (self.startup is not None):
            await self.startup.acquire(self.finish_event)

    async def spawn_child(self) -> AsyncIterator[events.AgentEvent]:
        self.launch_started_at = self.loop.time()
        self.session_bytes: int | None = None
        if self.session_file:
            with suppress(OSError):
                self.session_bytes = Path(self.session_file).stat().st_size
        if self.reused:
            assert self.persistent_session is not None and self.persistent_session.proc is not None
            self.proc = self.persistent_session.proc
            self.spawn_ms = 0
        else:
            try:
                self.proc = await AttachedChild.start(
                    self.launch.argv,
                    cwd=self.cwd,
                    env=self.launch.env,
                )
            except OSError as exc:
                yield events.Done(text=f"agent launch failed: {exc}", ok=False)
                self.finished = True
                return
            self.spawn_ms = round((self.loop.time() - self.launch_started_at) * 1000)
        self.owner = asyncio.current_task()
        if self.owner is not None:
            _ACTIVE_PROCESSES[self.owner] = self.proc
        assert self.proc.stdout is not None
        assert self.proc.stderr is not None
        self.stderr_task = (
            self.persistent_session.stderr_task
            if self.reused and self.persistent_session is not None
            else asyncio.create_task(self.stderr_tail())
        )
        assert self.stderr_task is not None
        if self.owner is not None:
            _ACTIVE_STDERR_TASKS[self.owner] = self.stderr_task
        self.prompt_dispatched = False
        if self.stdin_payload is not None and self.proc.stdin is not None:
            try:
                self.prompt_dispatched = not self.require_input_id
                self.proc.stdin.write(self.stdin_payload)
                await self.proc.stdin.drain()
            except (BrokenPipeError, ConnectionResetError):
                pass

    def initialize_output(self) -> None:
        self.text_parts: list[str] = []
        self.assistant_message_parts: list[str] = []
        self.ok = True
        self.failure = None
        self.diagnostic: dict[str, int] = {}
        self.preflight_failure: str | None = None
        self.error_message: str | None = None
        self.image_input_sent = bool(self.images)
        self.inherited_image_sensitive = bool(
            self.reused
            and self.persistent_session is not None
            and self.persistent_session.sensitive_diagnostics
        )
        assert self.proc.stdout is not None

    async def initialize_rpc(self) -> AsyncIterator[events.AgentEvent]:
        self.steering_task: asyncio.Task[None] | None = None
        self.explicit_interrupt = False
        self.rejected_commands: list[events.AgentEvent] = []
        self.rejected_signal = asyncio.Event()
        self.session_identity_uncertain = False
        self.final_assistant_stop = False
        if self.steering_queue is not None and self.proc.stdin is not None:
            self.stdin = self.proc.stdin
            if not self.require_input_id:
                self.steering_task = asyncio.create_task(self.inputs.forward(self))
                if self.owner is not None:
                    _ACTIVE_STEERING[self.owner] = self.steering_task
        self.model_name: str | None = None
        self.session_name: str | None = None
        self.active_session_file = self.session_file
        self.initial_session_id: str | None = None
        self.initial_session_file: str | None = None
        self.initial_session_observed = False
        self.initial_prompt_acknowledged = False
        self.native_capability_confirmed = not self.require_input_id
        self.prompt_start_deadline: float | None = None
        self.initial_input_started = False
        self.live_status_seen = False
        self.settlement_count = 0
        self.agent_settled_seen = False
        self.reader = (
            self.persistent_session.reader
            if self.reused and self.persistent_session is not None
            else PiRpcChannel(self.proc.stdout)
        )
        assert self.reader is not None
        self.reader.pending.cancel_all()
        self.reader.pending.add(
            commands.GetState, self.preflight_id, request=commands.GetState(id=self.preflight_id)
        )
        self.reader.pending.add(
            commands.Prompt,
            self.prompt_id,
            request=commands.Prompt(
                id=self.prompt_id, input_id=self.original_input_id, message=self.task
            ),
        )
        self.preflight_wait_started_at = self.loop.time()
        self.preflight_budget = NATIVE_STARTUP_POLICY.readiness_timeout(
            self.session_bytes, base_seconds=CAPABILITY_PREFLIGHT_TIMEOUT_SECONDS
        )
        self.preflight_deadline = self.preflight_wait_started_at + self.preflight_budget
        if not self.require_input_id:
            self.prompt_start_deadline = self.loop.time() + PROMPT_START_TIMEOUT_SECONDS
        self.last_model_progress = self.loop.time()
        self.phase = phases.PromptAcceptancePhase()
        self.prompt_accepted = False
        self.active_tools: set[str] = set()
        self.tool_ever_started = False
        self.output_started = False
        self.compaction_started = False
        self.retry_recovery_pending = False
        self.retry_recovery_reason = "provider_auto_retry_progress"
        self.started_during_abort: list[str | None] = []
        self.ui_seen: set[str] = set()
        if self.owner is not None:
            _ACTIVE_INPUT_RESTORERS[self.owner] = lambda: self.inputs.restore(self)
        if False:
            yield

    async def retain_or_close(self) -> AsyncIterator[events.AgentEvent]:
        if self.steering_task is not None:
            self.steering_task.cancel()
            await asyncio.gather(self.steering_task, return_exceptions=True)
        self.unresolved_inputs = bool(self.inputs.pending) or (
            self.steering_queue is not None and (not self.steering_queue.empty())
        )
        self.inputs.restore(self)
        self.reader.pending.cancel_all()
        if self.owner is not None:
            _ACTIVE_INPUT_RESTORERS.pop(self.owner, None)
        self.revision = _session_revision(self.active_session_file)
        self.retained = self.can_retain()
        if self.retained:
            assert self.persistent_session is not None
            self.persistent_session.proc = self.proc
            self.persistent_session.reader = self.reader
            self.persistent_session.stderr_task = self.stderr_task
            self.persistent_session.launch_key = self.launch_key
            self.persistent_session.session_file = self.active_session_file
            self.persistent_session.session_id = self.initial_session_id
            self.persistent_session.revision = self.revision
            self.persistent_session.sensitive_diagnostics = (
                self.image_input_sent or self.inherited_image_sensitive
            )
            if self.validated_session_id is not None:
                self.persistent_session.reopen_required = None
                self.persistent_session.reopen_session_id = None
        else:
            outcome = await self.proc.finish()
            if isinstance(outcome, TimedOutOutcome):
                self.ok = False
                self.record_failure(failures.BackendDidNotExit("agent backend did not exit"))
        if False:
            yield

    def can_retain(self) -> bool:
        return bool(
            self.persistent_session is not None
            and self.require_input_id
            and (self.proc.returncode is None)
            and self.ok
            and (not self.fail_reason)
            and (self.error_message is None)
            and (not self.session_identity_uncertain)
            and (not isinstance(self.failure, failures.InputIdUnavailable))
            and (not self.inputs.uncertain)
            and (not self.unresolved_inputs)
            and self.initial_input_started
            and self.initial_prompt_acknowledged
            and self.final_assistant_stop
            and self.agent_settled_seen
            and self.stats.complete
            and isinstance(self.initial_session_id, str)
            and isinstance(self.initial_session_file, str)
            and (self.initial_session_file == self.active_session_file)
            and (self.revision is not None)
        )

    async def finish_diagnostics(self) -> AsyncIterator[events.AgentEvent]:
        if self.owner is not None:
            _ACTIVE_PROCESSES.pop(self.owner, None)
        self.error_text = "" if self.retained else await self.stderr_task
        if (
            self.preflight_failure == FailureReason.PREFLIGHT_EXIT
            and self.error_text.strip()
            and not (self.image_input_sent or self.inherited_image_sensitive)
        ):
            self.record_failure(
                failures.InputIdUnavailable(
                    "Pi native input-ID capability preflight ended before attestation. "
                    "The prompt was not sent. Backend startup reported:\n" + self.error_text.strip()
                )
            )
        if self.owner is not None:
            _ACTIVE_STDERR_TASKS.pop(self.owner, None)
        if not self.retained and self.reused and (self.persistent_session is not None):
            await self.persistent_session.close()
        if False:
            yield

    async def finish_result(self) -> AsyncIterator[events.AgentEvent]:
        self.transport_successful = (
            self.ok and (not self.fail_reason) and (self.retained or self.proc.returncode == 0)
        )
        self.otherwise_successful = self.transport_successful and self.error_message is None
        if self.otherwise_successful and (not self.session_identity_uncertain):
            if not self.initial_input_started:
                self.record_failure(
                    failures.InputMissing(
                        "Pi RPC run ended without this prompt's user message start."
                    )
                )
            elif not self.final_assistant_stop:
                self.record_failure(
                    failures.FinalStopMissing(
                        "Pi RPC run ended without an authoritative final assistant stop."
                    )
                )
            elif self.unresolved_inputs:
                self.record_failure(
                    failures.QueuedInputMissing(
                        "Pi RPC run ended with an unstarted queued input; delivery is uncertain."
                    )
                )
        self.success = (
            self.otherwise_successful
            and self.initial_input_started
            and self.final_assistant_stop
            and (not self.inputs.uncertain)
            and (not self.unresolved_inputs)
        )
        self.terminal_reason_code: str | None = None
        if (self.transport_successful or self.inputs.uncertain or self.fail_reason) and (
            not self.initial_input_started
        ):
            self.record_failure(
                failures.InputMissing(
                    self.fail_reason or "Pi RPC run ended without this prompt's user message start."
                )
            )
        if self.transport_successful and (not self.final_assistant_stop):
            self.record_failure(
                failures.FinalStopMissing(
                    self.fail_reason
                    or self.error_message
                    or "Pi RPC run ended without an authoritative final assistant stop."
                )
            )
        if self.otherwise_successful and self.unresolved_inputs:
            self.record_failure(
                failures.QueuedInputMissing(
                    self.fail_reason
                    or "Pi RPC run ended with an unstarted queued input; delivery is uncertain."
                )
            )
        self.terminal_reason_code = self.failure.code if self.failure else None
        yield events.Done(
            text=(
                self.failure.text
                if self.failure is not None
                else (
                    "".join(self.text_parts).strip()
                    if self.success
                    else self.error_message
                    or (
                        "Image prompt failed; backend diagnostics withheld."
                        if (self.image_input_sent or self.inherited_image_sensitive)
                        and self.error_text
                        else self.error_text
                    )
                    or f"Backend exited with code {self.proc.returncode}"
                )
            ),
            ok=self.success and (not self.session_identity_uncertain),
            reason_code=self.terminal_reason_code,
            diagnostic={
                **self.diagnostic,
                **({"reason": self.preflight_failure} if self.preflight_failure else {}),
                **({"exit_code": self.proc.returncode} if self.proc.returncode is not None else {}),
            },
        )

    async def handle_timeout(self) -> AsyncIterator[events.AgentEvent]:
        if self.require_input_id and (not self.native_capability_confirmed):
            self.elapsed_ms = round((self.loop.time() - self.launch_started_at) * 1000)
            self.wait_ms = round((self.loop.time() - self.preflight_wait_started_at) * 1000)
            self.preflight_failure = FailureReason.PREFLIGHT_TIMEOUT
            self.diagnostic = {
                "elapsed_ms": self.elapsed_ms,
                "wait_ms": self.wait_ms,
                "spawn_ms": self.spawn_ms,
                "budget_ms": round(self.preflight_budget * 1000),
            }
            if self.session_bytes is not None:
                self.diagnostic["session_bytes"] = self.session_bytes
            self.session_size = self.session_bytes if self.session_bytes is not None else "unknown"
            self.record_failure(
                failures.InputIdUnavailable(
                    "Pi native input-ID capability preflight timed out "
                    f"(phase=await_get_state, elapsed_ms={self.elapsed_ms}, "
                    f"wait_ms={self.wait_ms}, budget_ms={round(self.preflight_budget * 1000)}, "
                    f"spawn_ms={self.spawn_ms}, session_bytes={self.session_size})."
                )
            )
            await self.proc.stop()
            self.finished = True
            return
        if (
            self.prompt_start_deadline is not None
            and (not self.initial_input_started)
            and (not self.phase.pauses_input_clock)
        ):
            self.record_failure(
                failures.InputMissing("Pi RPC run ended without this prompt's user message start.")
            )
            await self.proc.stop()
            self.finished = True
            return
        if self.stats.requested:
            self.finished = True
            return
        self.elapsed_ms = round((self.loop.time() - self.last_model_progress) * 1000)
        self.reason_code, self.stalled_phase = self.phase.stalled(self.prompt_accepted)
        if self.prompt_accepted:
            yield self.turn_state(
                "model_stalled", self.reason_code, self.elapsed_ms, event_phase=self.stalled_phase
            )
        yield self.turn_state("aborting", self.reason_code, self.elapsed_ms, event_phase="shutdown")
        await self.abort_stalled_rpc()
        for input_id in self.started_during_abort:
            yield events.InputStarted(id=input_id)
        self.failed_elapsed_ms = round((self.loop.time() - self.last_model_progress) * 1000)
        yield self.turn_state(
            "failed", self.reason_code, self.failed_elapsed_ms, event_phase="shutdown"
        )
        self.record_failure(
            failures.ModelStalled(
                f"Model produced no RPC progress for {self.model_wait_timeout:g} seconds."
                if self.prompt_accepted
                else f"Pi did not accept the prompt within {self.model_wait_timeout:g} seconds."
            )
        )
        self.finished = True
        self.finished = True
        return
