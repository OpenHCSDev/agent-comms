"""Agent communications — streaming agent backend runner.

Runs a headless agent (pi by default) and yields structured events. Two
modes:

- **rpc**: pi's ``--mode rpc`` JSON-lines stream gives real-time events —
  text deltas, tool calls with arguments, tool results. These drive
  opencode-style feedback (thinking spinners, tool-call cards).
- **text** (fallback): any other backend; output arrives as raw chunks.

Events (dicts):
    {"type": "chunk",      "text": str}
    {"type": "tool_start", "id": str, "name": str, "title": str}
    {"type": "tool_end",   "id": str, "name": str, "ok": bool, "output": str}
    {"type": "agent_info", "model": str | None, "context_used": int | None,
                            "context_size": int | None, "session_name": str | None}
    {"type": "turn_state", "state": "model_stalled" | "aborting" |
                                     "retrying" | "recovered" | "failed", ...}
    {"type": "done",       "text": str, "ok": bool}

The runner never raises on backend failure; it yields ``done`` with
``ok=False`` and the error as text. Callers own presentation. Provider
failures that Pi reports as a completed assistant message (for example an
exhausted usage limit) also fail the turn, carrying ``errorMessage``.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import signal
from collections.abc import AsyncGenerator, AsyncIterator, Callable, Sequence
from contextlib import aclosing
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .image_inputs import ImageInput
from .tool_results import ToolDiff

RPC_FLAG = "--mode"
RPC_VALUE = "rpc"
_TOOL_KINDS = {
    "bash": "execute",
    "read": "read",
    "write": "edit",
    "edit": "edit",
    "grep": "search",
    "glob": "search",
}
_ACTIVE_PROCESSES: dict[asyncio.Task[Any], asyncio.subprocess.Process] = {}
_ACTIVE_STEERING: dict[asyncio.Task[Any], asyncio.Task[None]] = {}
_ACTIVE_INPUT_RESTORERS: dict[asyncio.Task[Any], Callable[[], None]] = {}
# Pi 0.85.1 owns provider-idle detection and defaults it to 300 seconds. This
# transport backstop must remain strictly longer so Pi can emit its authoritative
# timeout, auto-retry, and transport evidence before agent-comms intervenes.
_PI_0_85_1_PROVIDER_IDLE_TIMEOUT_SECONDS = 300.0
MODEL_WAIT_TIMEOUT_SECONDS = 360.0
assert MODEL_WAIT_TIMEOUT_SECONDS > _PI_0_85_1_PROVIDER_IDLE_TIMEOUT_SECONDS
RPC_ABORT_GRACE_SECONDS = 2.0
_SESSION_MUTATING_COMMANDS = frozenset({"new_session", "switch_session", "fork", "clone"})
_IDENTITY_FAILURE_TEXT = "Pi session identity changed during this turn."


class _JsonLineReader:
    """Read whole JSONL records regardless of asyncio's transport buffer limit.

    Pi's end-of-turn events may contain many messages in one record. Retain
    consumed fragments across cancellation when the finish signal wins the
    read race, so the next read can finish the same record without losing bytes.
    """

    def __init__(self, reader: asyncio.StreamReader):
        self.reader = reader
        self.chunks: list[bytes] = []

    async def readline(self) -> bytes:
        while True:
            try:
                line = await self.reader.readuntil(b"\n")
            except asyncio.LimitOverrunError as error:
                self.chunks.append(await self.reader.readexactly(error.consumed))
                continue
            except asyncio.IncompleteReadError as error:
                line = error.partial
            self.chunks.append(line)
            record = b"".join(self.chunks)
            self.chunks.clear()
            return record


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


async def _terminate_process(proc: asyncio.subprocess.Process) -> None:
    """Terminate one process and its POSIX process group."""
    if proc.stdin is not None:
        proc.stdin.close()
    if proc.returncode is not None:
        return
    try:
        if os.name == "posix":
            os.killpg(proc.pid, signal.SIGTERM)
        else:
            proc.terminate()
    except ProcessLookupError:
        return
    try:
        await asyncio.wait_for(proc.wait(), timeout=1.0)
    except TimeoutError:
        try:
            if os.name == "posix":
                os.killpg(proc.pid, signal.SIGKILL)
            else:
                proc.kill()
        except ProcessLookupError:
            return
        await proc.wait()


async def terminate_task_process(task: asyncio.Task[Any]) -> None:
    """Terminate the backend subprocess owned by an agent turn task."""
    if steering := _ACTIVE_STEERING.pop(task, None):
        steering.cancel()
        await asyncio.gather(steering, return_exceptions=True)
    if restore_inputs := _ACTIVE_INPUT_RESTORERS.pop(task, None):
        restore_inputs()
    proc = _ACTIVE_PROCESSES.pop(task, None)
    if proc is not None:
        await _terminate_process(proc)


def rpc_args_for(bin_name: str, args: Sequence[str]) -> list[str] | None:
    """RPC-mode args for a backend, or None if the backend should run in text mode.

    pi gets ``--mode rpc`` inserted; the prompt arrives on stdin as a JSON
    ``{"type": "prompt", "message": ...}`` line.
    """
    if Path(bin_name).name.startswith("pi") or bin_name.endswith("/pi"):
        return [*args, RPC_FLAG, RPC_VALUE]
    return None


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
    if rpc_args_for(agent_bin, agent_args) is None:
        return ["off"]
    args = args_for_model(agent_args, model)
    rpc_args = rpc_args_for(agent_bin, args)
    assert rpc_args is not None
    argv = [
        agent_bin,
        *rpc_args,
        "--no-extensions",
        "--no-skills",
        "--no-context-files",
        "--no-session",
    ]
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            limit=1024 * 1024,
        )
    except OSError:
        return ["off"]
    levels: list[str] = []
    try:
        assert proc.stdin is not None and proc.stdout is not None
        proc.stdin.write(
            (
                json.dumps({"id": "thinking", "type": "get_available_thinking_levels"}) + "\n"
            ).encode()
        )
        await proc.stdin.drain()
        reader = _JsonLineReader(proc.stdout)
        async with asyncio.timeout(10):
            while line := await reader.readline():
                payload = json.loads(line)
                if payload.get("id") != "thinking" or payload.get("type") != "response":
                    continue
                values = (payload.get("data") or {}).get("levels", [])
                levels = [str(value) for value in values if isinstance(value, str)]
                break
    except (TimeoutError, ValueError, OSError):
        pass
    finally:
        if proc.stdin is not None:
            proc.stdin.close()
        try:
            await asyncio.wait_for(proc.wait(), timeout=1)
        except TimeoutError:
            proc.terminate()
            await proc.wait()
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
    elif (rpc_args := rpc_args_for(agent_bin, agent_args)) is None:
        values = []
    else:
        argv = [agent_bin, *rpc_args, "--no-extensions", "--no-skills", "--no-context-files"]
        if "--no-session" not in argv:
            argv.append("--no-session")
        try:
            proc = await asyncio.create_subprocess_exec(
                *argv,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
                limit=8 * 1024 * 1024,
            )
        except OSError:
            proc = None
        values = []
        if proc is not None:
            try:
                assert proc.stdin is not None and proc.stdout is not None
                reader = _JsonLineReader(proc.stdout)
                proc.stdin.write(
                    (json.dumps({"id": "models", "type": "get_available_models"}) + "\n").encode()
                )
                await proc.stdin.drain()
                async with asyncio.timeout(10):
                    while line := await reader.readline():
                        payload = json.loads(line)
                        if payload.get("id") != "models" or payload.get("type") != "response":
                            continue
                        for item in (payload.get("data") or {}).get("models", []):
                            provider = item.get("provider")
                            model_id = item.get("id")
                            if provider and model_id:
                                values.append(f"{provider}/{model_id}")
                        break
            except (TimeoutError, ValueError, OSError):
                pass
            finally:
                if proc.stdin is not None:
                    proc.stdin.close()
                try:
                    await asyncio.wait_for(proc.wait(), timeout=1)
                except TimeoutError:
                    proc.terminate()
                    try:
                        await asyncio.wait_for(proc.wait(), timeout=1)
                    except TimeoutError:
                        proc.kill()
                        await proc.wait()
    if selected and selected not in values:
        values.insert(0, selected)
    return [Model(value, value) for value in dict.fromkeys(values)]


async def compact_session(
    agent_bin: str,
    agent_args: Sequence[str],
    session_file: str,
    cwd: str,
    custom_instructions: str | None = None,
) -> dict[str, Any]:
    """Run Pi's native manual compaction against one persisted session."""
    rpc_args = rpc_args_for(agent_bin, agent_args)
    if rpc_args is None:
        return {"ok": False, "error": "This backend does not support context compaction."}
    if not session_file or not Path(session_file).is_file():
        return {"ok": False, "error": "This thread has no persisted session to compact."}

    argv = [agent_bin, *rpc_args, "--session", session_file]
    env = os.environ.copy()
    if env.get("AGENT_COMMS_MANAGED") == "1":
        env["PI_WORKTREE"] = str(Path(cwd).resolve())
        bootstrap = Path(__file__).with_name("pi_project_bootstrap.mjs").resolve().as_uri()
        flag = f"--import={bootstrap}"
        options = env.get("NODE_OPTIONS", "")
        if flag not in options:
            env["NODE_OPTIONS"] = f"{options} {flag}".strip()
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            cwd=cwd if Path(cwd).is_dir() else None,
            env=env,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=os.name == "posix",
        )
    except OSError as error:
        return {"ok": False, "error": f"Unable to start compaction: {error}"}

    assert proc.stdin is not None and proc.stdout is not None and proc.stderr is not None
    stderr_task = asyncio.create_task(proc.stderr.read())
    command: dict[str, Any] = {"id": "compact", "type": "compact"}
    if custom_instructions:
        command["customInstructions"] = custom_instructions
    result: dict[str, Any] = {"ok": False, "error": "Compaction process ended unexpectedly."}
    try:
        proc.stdin.write((json.dumps(command) + "\n").encode())
        await proc.stdin.drain()
        reader = _JsonLineReader(proc.stdout)
        async with asyncio.timeout(300):
            while line := await reader.readline():
                payload = json.loads(line)
                if payload.get("type") != "response" or payload.get("id") != "compact":
                    continue
                if payload.get("success"):
                    result = {"ok": True, **(payload.get("data") or {})}
                else:
                    result = {"ok": False, "error": payload.get("error") or "Compaction failed."}
                break
    except TimeoutError:
        result = {"ok": False, "error": "Compaction timed out."}
    except (ValueError, OSError) as error:
        result = {"ok": False, "error": f"Compaction failed: {error}"}
    finally:
        proc.stdin.close()
        try:
            await asyncio.wait_for(proc.wait(), timeout=5)
        except TimeoutError:
            await _terminate_process(proc)
        stderr = (await stderr_task).decode(errors="replace").strip()
        if (
            not result["ok"]
            and stderr
            and result["error"] == "Compaction process ended unexpectedly."
        ):
            result["error"] = stderr[-4000:]
    return result


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


def _result_text(result: Any, limit: int = 4000) -> str:
    if not isinstance(result, dict):
        return ""
    text = "".join(
        block.get("text", "") for block in (result.get("content") or []) if isinstance(block, dict)
    )
    return text[:limit] + ("…" if len(text) > limit else "")


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
) -> AsyncIterator[dict[str, Any]]:
    """Run the backend and yield events. Always ends with a ``done`` event.

    The no-progress watchdog applies only while waiting for the model. A running
    tool has no deadline in this intentionally incomplete first slice: preventing
    false model-stall kills does not solve a hung tool, which requires a separate
    durable tool-liveness policy. Durable retry decisions belong to the
    coordinator, never this transport adapter.
    """
    owner = asyncio.current_task()
    try:
        async with aclosing(
            _stream_agent_events(
                agent_bin,
                agent_args,
                task,
                cwd,
                env_extra,
                session_file=session_file,
                steering_queue=steering_queue,
                finish_event=finish_event,
                fork_session=fork_session,
                images=images,
                model_wait_timeout=model_wait_timeout,
                rpc_abort_grace=rpc_abort_grace,
            )
        ) as events:
            async for event in events:
                yield event
    except Exception as error:
        yield {"type": "done", "ok": False, "text": f"Agent backend stream failed: {error}"}
    finally:
        if owner is not None:
            await terminate_task_process(owner)


async def _stream_agent_events(
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
) -> AsyncGenerator[dict[str, Any], None]:
    if shutil.which(agent_bin) is None and not Path(agent_bin).is_file():
        yield {"type": "done", "text": f"agent backend {agent_bin!r} not found", "ok": False}
        return

    rpc_args = rpc_args_for(agent_bin, agent_args)
    if images and rpc_args is None:
        yield {"type": "done", "text": "This backend does not support image prompts.", "ok": False}
        return
    stdin_payload: bytes | None = None
    argv: list[str]
    if rpc_args is not None:
        argv = [agent_bin, *rpc_args]
        if session_file:
            argv += ["--fork" if fork_session else "--session", session_file]
        stdin_payload = (
            json.dumps({"type": "get_state"})
            + "\n"
            + json.dumps(
                {
                    "id": "agent-comms-prompt",
                    "type": "prompt",
                    "message": task,
                    **({"images": [image.to_rpc() for image in images]} if images else {}),
                }
            )
            + "\n"
        ).encode()
    else:
        argv = [agent_bin, *agent_args, task]

    env = os.environ.copy()
    if env_extra:
        env.update(env_extra)
    if rpc_args is not None and env.get("AGENT_COMMS_MANAGED") == "1":
        env["PI_WORKTREE"] = str(Path(cwd).resolve())
        bootstrap = Path(__file__).with_name("pi_project_bootstrap.mjs").resolve().as_uri()
        flag = f"--import={bootstrap}"
        options = env.get("NODE_OPTIONS", "")
        if flag not in options:
            env["NODE_OPTIONS"] = f"{options} {flag}".strip()
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            cwd=cwd if Path(cwd).is_dir() else None,
            env=env,
            stdin=(
                asyncio.subprocess.PIPE if stdin_payload is not None else asyncio.subprocess.DEVNULL
            ),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=os.name == "posix",
        )
    except OSError as exc:
        yield {"type": "done", "text": f"agent launch failed: {exc}", "ok": False}
        return

    owner = asyncio.current_task()
    if owner is not None:
        _ACTIVE_PROCESSES[owner] = proc

    assert proc.stdout is not None
    assert proc.stderr is not None

    async def stderr_tail() -> str:
        tail = b""
        assert proc.stderr is not None
        while chunk := await proc.stderr.read(4096):
            tail = (tail + chunk)[-16_000:]
        return tail.decode(errors="replace").strip()

    stderr_task = asyncio.create_task(stderr_tail())
    if stdin_payload is not None and proc.stdin is not None:
        # pi's rpc protocol keeps stdin open while it streams; closing it
        # after the prompt makes the backend exit before responding.
        try:
            proc.stdin.write(stdin_payload)
            await proc.stdin.drain()
        except (BrokenPipeError, ConnectionResetError):
            pass

    text_parts: list[str] = []
    ok = True
    fail_reason = ""
    error_message: str | None = None
    assert proc.stdout is not None

    if rpc_args is None:
        while True:
            chunk = await proc.stdout.read(4096)
            if not chunk:
                break
            piece = chunk.decode(errors="replace")
            text_parts.append(piece)
            yield {"type": "chunk", "text": piece}
        code = await proc.wait()
        if owner is not None:
            _ACTIVE_PROCESSES.pop(owner, None)
        error_text = await stderr_task
        yield {
            "type": "done",
            "text": (
                "".join(text_parts).strip()
                if code == 0
                else error_text or f"Backend exited with code {code}"
            ),
            "ok": code == 0,
        }
        return

    steering_task: asyncio.Task[None] | None = None
    pending_inputs: list[tuple[str | None, str, str | dict[str, Any]]] = []
    rejected_commands: list[dict[str, Any]] = []
    rejected_signal = asyncio.Event()
    session_identity_uncertain = False
    if steering_queue is not None and proc.stdin is not None:
        stdin = proc.stdin

        async def forward_steering() -> None:
            while True:
                message = await steering_queue.get()
                original = dict(message) if isinstance(message, dict) else message
                command = (
                    dict(original)
                    if isinstance(original, dict)
                    else {"type": "prompt", "message": original, "streamingBehavior": "followUp"}
                )
                if command.get("type") == "prompt":
                    pending_inputs.append(
                        (command.pop("_input_id", None), command["message"], original)
                    )
                if command.get("type") in _SESSION_MUTATING_COMMANDS:
                    # Reject before writing: Pi may tear down A and bind B even
                    # before its RPC response. Rejection is not a failed A turn.
                    rejected_commands.append(
                        {
                            "type": "error",
                            "reason_code": "steering_command_rejected",
                            "command": command["type"],
                            "id": command.get("id"),
                            "text": f"Mid-turn {command['type']} is not supported.",
                        }
                    )
                    rejected_signal.set()
                    continue
                stdin.write((json.dumps(command) + "\n").encode())
                await stdin.drain()

        steering_task = asyncio.create_task(forward_steering())
        if owner is not None:
            _ACTIVE_STEERING[owner] = steering_task

    # RPC mode: JSON lines with agent events. Keep stdin open after the turn
    # long enough to ask Pi for its authoritative current context estimate.
    model_name: str | None = None
    session_name: str | None = None
    active_session_file = session_file
    initial_session_id: str | None = None
    initial_session_file: str | None = None
    initial_session_observed = False
    context_used: int | None = None
    context_size: int | None = None
    confirmed_context_used: int | None = None
    provisional_usage = False
    # A prompt ACK can mean handled/queued, and a final from an unrelated run
    # cannot complete this prompt. Observe this prompt's user message first.
    initial_prompt_acknowledged = False
    initial_input_started = False
    final_assistant_stop = False
    stats_requested = False
    reader = _JsonLineReader(proc.stdout)
    loop = asyncio.get_running_loop()
    last_model_progress = loop.time()
    phase = "prompt_acceptance"
    prompt_accepted = False
    active_tools: set[str] = set()
    tool_ever_started = False
    output_started = False
    forwarded_input_started = False
    compaction_started = False
    retry_recovery_pending = False
    retry_recovery_reason = "provider_auto_retry_progress"
    started_during_abort: list[str | None] = []

    async def request_stats() -> None:
        nonlocal stats_requested
        if proc.stdin is None or stats_requested:
            return
        stats_requested = True
        try:
            proc.stdin.write((json.dumps({"type": "get_state"}) + "\n").encode())
            proc.stdin.write((json.dumps({"type": "get_session_stats"}) + "\n").encode())
            await proc.stdin.drain()
        except (BrokenPipeError, ConnectionResetError):
            pass

    async def read_rpc_line(timeout: float | None) -> bytes:
        # A rejected command must be visible even while Pi is in a tool or
        # model wait (both may have an unbounded read timeout).
        if rejected_signal.is_set():
            return b"\n"
        read_task = asyncio.create_task(reader.readline())
        reject_task = asyncio.create_task(rejected_signal.wait())
        finish_task = (
            asyncio.create_task(finish_event.wait())
            if finish_event is not None and not stats_requested
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
            # Do not discard an already-read record when two signals race.
            if read_task in done:
                return read_task.result()
            if finish_task is not None and finish_task in done:
                # Cancel the pending read before request_stats can yield; a
                # completed-but-discarded record would lose Pi evidence.
                read_task.cancel()
                await asyncio.gather(read_task, return_exceptions=True)
                await request_stats()
            return b"\n"
        finally:
            for pending in tasks:
                if not pending.done():
                    pending.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    def mark_pending_input_started(payload: dict[str, Any]) -> tuple[bool, str | None]:
        nonlocal forwarded_input_started
        message = payload.get("message") or {}
        if message.get("role") != "user":
            return False, None
        content = message.get("content", "")
        text = (
            content
            if isinstance(content, str)
            else "\n".join(part.get("text", "") for part in content if part.get("type") == "text")
        )
        for index, (input_id, queued_text, _) in enumerate(pending_inputs):
            if text.strip() == queued_text.strip():
                pending_inputs.pop(index)
                forwarded_input_started = True
                return True, input_id
        return False, None

    def restore_pending_inputs() -> None:
        if steering_queue is None or not pending_inputs:
            return
        queued: list[str | dict[str, Any]] = []
        while not steering_queue.empty():
            queued.append(steering_queue.get_nowait())
        for _, _, original in pending_inputs:
            steering_queue.put_nowait(original)
        for item in queued:
            steering_queue.put_nowait(item)
        pending_inputs.clear()

    if owner is not None:
        _ACTIVE_INPUT_RESTORERS[owner] = restore_pending_inputs

    async def abort_stalled_rpc() -> None:
        nonlocal compaction_started, tool_ever_started, output_started
        if steering_task is not None:
            steering_task.cancel()
            await asyncio.gather(steering_task, return_exceptions=True)
        if proc.stdin is not None and proc.returncode is None:
            abort_deadline = loop.time() + max(0.0, rpc_abort_grace)
            try:
                proc.stdin.write(
                    (json.dumps({"id": "agent-comms-watchdog", "type": "abort"}) + "\n").encode()
                )
                remaining = max(0.0, abort_deadline - loop.time())
                await asyncio.wait_for(proc.stdin.drain(), timeout=remaining)
                while remaining := max(0.0, abort_deadline - loop.time()):
                    response = await asyncio.wait_for(reader.readline(), timeout=remaining)
                    if not response:
                        break
                    try:
                        abort_payload = json.loads(response)
                    except json.JSONDecodeError:
                        continue
                    abort_kind = abort_payload.get("type")
                    if abort_kind == "message_start" and not session_identity_uncertain:
                        # A B-session user message cannot prove A's queued
                        # follow-up began. Preserve it for the old owner.
                        matched, input_id = mark_pending_input_started(abort_payload)
                        if matched:
                            started_during_abort.append(input_id)
                    elif abort_kind in {"tool_execution_start", "tool_execution_update"}:
                        tool_ever_started = True
                    elif abort_kind == "message_update":
                        delta = abort_payload.get("assistantMessageEvent") or {}
                        if delta.get("type") in {
                            "text_delta",
                            "thinking_delta",
                            "toolcall_start",
                            "toolcall_delta",
                            "toolcall_end",
                        } and (delta.get("delta") or delta.get("type", "").startswith("toolcall")):
                            output_started = True
                    elif abort_kind in {
                        "compaction_start",
                        "summarization_retry_scheduled",
                        "summarization_retry_attempt_start",
                    }:
                        compaction_started = True
                    if abort_kind == "response" and abort_payload.get("command") == "abort":
                        break
            except (TimeoutError, BrokenPipeError, ConnectionResetError):
                pass
        if proc.returncode is None:
            await _terminate_process(proc)

    def turn_state(
        state: str,
        reason_code: str,
        elapsed_ms: int,
        *,
        event_phase: str | None = None,
        attempt: tuple[int | None, int | None] | None = None,
    ) -> dict[str, Any]:
        replay_safe = not (
            tool_ever_started or output_started or forwarded_input_started or compaction_started
        )
        event: dict[str, Any] = {
            "type": "turn_state",
            "state": state,
            "reason_code": reason_code,
            "elapsed_ms": max(0, elapsed_ms),
            "phase": event_phase or phase,
            "retryable": replay_safe,
            "replay_safe": replay_safe,
            "side_effects_possible": (
                tool_ever_started or forwarded_input_started or compaction_started
            ),
        }
        if attempt is not None:
            event["attempt"] = {"current": attempt[0], "max": attempt[1]}
        return event

    def positive_tokens(usage: Any) -> int | None:
        if not isinstance(usage, dict):
            return None
        tokens = usage.get("totalTokens")
        return tokens if type(tokens) is int and tokens > 0 else None

    def context_info() -> dict[str, Any]:
        return {
            "type": "agent_info",
            "model": model_name,
            "session_name": session_name,
            "session_file": active_session_file,
            "context_used": context_used,
            "context_size": context_size,
        }

    def retry_made_progress(payload: dict[str, Any]) -> bool:
        kind = payload.get("type")
        message = payload.get("message") or {}
        if kind == "message_start" and message.get("role") == "assistant":
            return True
        if kind == "message_end" and message.get("role") == "assistant":
            return message.get("stopReason") not in {"error", "aborted"}
        if kind == "message_update":
            delta = payload.get("assistantMessageEvent") or {}
            return delta.get("type") in {
                "text_delta",
                "thinking_delta",
                "toolcall_start",
                "toolcall_delta",
                "toolcall_end",
            } and bool(delta.get("delta") or delta.get("type", "").startswith("toolcall"))
        return kind in {"tool_execution_start", "agent_settled"}

    while True:
        while rejected_commands:
            yield rejected_commands.pop(0)
        rejected_signal.clear()
        try:
            if stats_requested:
                read_timeout: float | None = 5.0
            elif active_tools or model_wait_timeout is None:
                read_timeout = None
            else:
                read_timeout = max(0.0, last_model_progress + model_wait_timeout - loop.time())
            line = await read_rpc_line(read_timeout)
        except TimeoutError:
            if stats_requested:
                break
            elapsed_ms = round((loop.time() - last_model_progress) * 1000)
            if not prompt_accepted:
                reason_code = "prompt_acceptance_timeout"
                stalled_phase = "prompt_acceptance"
            elif phase == "compaction":
                reason_code = "compaction_no_progress"
                stalled_phase = phase
            elif phase == "summarization_retry":
                reason_code = "summarization_retry_no_progress"
                stalled_phase = phase
            elif phase == "provider_retry":
                reason_code = "retry_no_progress"
                stalled_phase = phase
            else:
                reason_code = "model_no_progress"
                stalled_phase = "model_wait"
            if prompt_accepted:
                yield turn_state(
                    "model_stalled", reason_code, elapsed_ms, event_phase=stalled_phase
                )
            yield turn_state("aborting", reason_code, elapsed_ms, event_phase="shutdown")
            await abort_stalled_rpc()
            for input_id in started_during_abort:
                yield {"type": "input_started", "id": input_id}
            failed_elapsed_ms = round((loop.time() - last_model_progress) * 1000)
            yield turn_state("failed", reason_code, failed_elapsed_ms, event_phase="shutdown")
            fail_reason = (
                f"Model produced no RPC progress for {model_wait_timeout:g} seconds."
                if prompt_accepted
                else f"Pi did not accept the prompt within {model_wait_timeout:g} seconds."
            )
            break
        if not line:
            break
        line = line.strip()
        if not line:
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        kind = payload.get("type")
        command = payload.get("command")
        # Rejected commands never reach Pi. Any session-changing response we
        # nevertheless observe is unsolicited; even a failed/cancelled response
        # cannot prove Pi kept A bound. Check state/stats BEFORE projecting any
        # metadata, text, tool, retry, settlement, or provider error from B.
        data = payload.get("data")
        identity_changed = kind == "response" and command in _SESSION_MUTATING_COMMANDS
        if (
            kind == "response"
            and payload.get("success")
            and isinstance(data, dict)
            and command in {"get_state", "get_session_stats"}
            and initial_session_observed
        ):
            identity_changed = identity_changed or bool(
                (
                    initial_session_id
                    and data.get("sessionId")
                    and data["sessionId"] != initial_session_id
                )
                or (
                    initial_session_file
                    and data.get("sessionFile")
                    and data["sessionFile"] != initial_session_file
                )
            )
        if identity_changed:
            session_identity_uncertain = True
            context_used = None
            confirmed_context_used = None
            provisional_usage = False
            text_parts.clear()
            yield context_info()  # Only A's previously observed metadata.
            yield {
                "type": "turn_state",
                "state": "failed",
                "reason_code": "session_identity_uncertain",
                "elapsed_ms": 0,
                "phase": "shutdown",
                "retryable": False,
                "replay_safe": False,
                "side_effects_possible": True,
            }
            fail_reason = _IDENTITY_FAILURE_TEXT
            await abort_stalled_rpc()
            break
        now = loop.time()
        initial_prompt_response = (
            kind == "response" and command == "prompt" and payload.get("id") == "agent-comms-prompt"
        )
        if retry_recovery_pending and retry_made_progress(payload):
            if kind in {"message_start", "message_end", "message_update"}:
                output_started = True
            elif kind == "tool_execution_start":
                tool_ever_started = True
            retry_recovery_pending = False
            yield turn_state("recovered", retry_recovery_reason, 0, event_phase="model_wait")
        if kind in {"agent_start", "agent_end", "message_start", "message_update", "message_end"}:
            prompt_accepted = True
            if (
                kind == "agent_start"
                or (kind == "agent_end" and payload.get("willRetry") is True)
                or (
                    kind == "message_start"
                    and (payload.get("message") or {}).get("role") in {"user", "assistant"}
                )
            ):
                # A new run/message invalidates the previous final stop until
                # this run itself ends with an authoritative assistant stop.
                final_assistant_stop = False
            last_model_progress = now
            if not active_tools and phase not in {
                "compaction",
                "summarization_retry",
                "provider_retry",
            }:
                phase = "model_wait"
        if initial_prompt_response:
            last_model_progress = now
            if payload.get("success"):
                initial_prompt_acknowledged = True
                prompt_accepted = True
                phase = "model_wait"
            else:
                error_message = str(payload.get("error") or "Prompt was rejected")
                yield turn_state("failed", "prompt_rejected", 0, event_phase="shutdown")
                yield {"type": "error", "text": error_message}
                break
        elif kind == "auto_retry_start":
            final_assistant_stop = False
            prompt_accepted = True
            retry_recovery_pending = True
            retry_recovery_reason = "provider_auto_retry_progress"
            elapsed_ms = round((now - last_model_progress) * 1000)
            last_model_progress = now
            phase = "provider_retry"
            current = payload.get("attempt") if isinstance(payload.get("attempt"), int) else None
            maximum = (
                payload.get("maxAttempts") if isinstance(payload.get("maxAttempts"), int) else None
            )
            yield turn_state(
                "retrying",
                "provider_auto_retry",
                elapsed_ms,
                event_phase="model_wait",
                attempt=(current, maximum),
            )
        elif kind == "auto_retry_end":
            last_model_progress = now
            phase = "model_wait"
            if payload.get("success"):
                # Acceptance/completion of Pi's retry command does not prove the
                # retried model produced anything. Keep RETRYING until model
                # progress or a successful settlement is observed.
                error_message = None
            else:
                retry_recovery_pending = False
                error_message = "Provider retry attempts were exhausted."
                yield turn_state("failed", "provider_retry_exhausted", 0, event_phase="model_wait")
        elif kind in {"summarization_retry_scheduled", "summarization_retry_attempt_start"}:
            compaction_started = True
            elapsed_ms = round((now - last_model_progress) * 1000)
            last_model_progress = now
            phase = "summarization_retry"
            current = payload.get("attempt") if isinstance(payload.get("attempt"), int) else None
            maximum = (
                payload.get("maxAttempts") if isinstance(payload.get("maxAttempts"), int) else None
            )
            yield turn_state(
                "retrying",
                "summarization_retry",
                elapsed_ms,
                event_phase="model_wait",
                attempt=(current, maximum),
            )
        elif kind == "summarization_retry_finished":
            last_model_progress = now
            phase = "model_wait"
            if payload.get("success") or payload.get("result"):
                yield turn_state(
                    "recovered", "summarization_retry_succeeded", 0, event_phase="model_wait"
                )
        elif kind == "compaction_start":
            compaction_started = True
            last_model_progress = now
            phase = "compaction"
        elif kind == "compaction_end":
            last_model_progress = now
            phase = "model_wait"
            if payload.get("result") is not None and payload.get("aborted") is False:
                # A committed compaction starts a new context epoch. Its summary
                # is not a measured token count; await a new assistant response.
                context_used = None
                confirmed_context_used = None
                provisional_usage = False
                yield context_info()
            if payload.get("willRetry"):
                final_assistant_stop = False
                retry_recovery_pending = True
                retry_recovery_reason = "overflow_retry_progress"
                yield turn_state(
                    "retrying", "overflow_compaction_retry", 0, event_phase="model_wait"
                )
        elif kind == "message_start" and (payload.get("message") or {}).get("role") == "user":
            message = payload["message"]
            content = message.get("content")
            if isinstance(content, str):
                user_text = content
            elif isinstance(content, list):
                user_text = "\n".join(
                    part.get("text", "")
                    for part in content
                    if isinstance(part, dict) and part.get("type") == "text"
                )
            else:
                user_text = None
            if initial_prompt_acknowledged and user_text == task:
                initial_input_started = True
            elif initial_input_started:
                # A subsequent, different user input needs its own assistant
                # final; it cannot complete the original prompt instead.
                initial_input_started = False
                final_assistant_stop = False
            matched, input_id = mark_pending_input_started(payload)
            if matched:
                yield {"type": "input_started", "id": input_id}
        elif kind == "response" and payload.get("command") == "set_model":
            yield {
                "type": "model_changed",
                "id": payload.get("id"),
                "ok": bool(payload.get("success")),
                "error": payload.get("error", "Model change failed"),
            }
        elif kind == "response" and payload.get("command") == "set_thinking_level":
            yield {
                "type": "thinking_changed",
                "id": payload.get("id"),
                "ok": bool(payload.get("success")),
                "error": payload.get("error", "Thinking level change failed"),
            }
        elif kind == "response" and payload.get("success"):
            command = payload.get("command")
            data = payload.get("data") or {}
            if command == "get_state":
                state_id = data.get("sessionId")
                state_file = data.get("sessionFile")
                if not initial_session_observed:
                    initial_session_id = state_id
                    initial_session_file = state_file
                    initial_session_observed = True
                model = data.get("model") or {}
                provider = model.get("provider")
                model_id = model.get("id") or model.get("name")
                model_name = (
                    f"{provider}/{model_id}" if provider and model_id else model_id or provider
                )
                session_name = data.get("sessionName")
                active_session_file = state_file or active_session_file
                context_size = model.get("contextWindow")
                yield {
                    "type": "agent_info",
                    "model": model_name,
                    "thinking_level": data.get("thinkingLevel"),
                    "session_name": session_name,
                    "session_file": active_session_file,
                    "context_used": context_used,
                    "context_size": context_size,
                }
            elif command == "get_session_stats":
                context = data.get("contextUsage") or {}
                tokens = context.get("tokens") if isinstance(context, dict) else None
                if type(tokens) is int and tokens > 0:
                    context_used = tokens
                    confirmed_context_used = tokens
                # Pi's positive stats already reflect its current branch and
                # post-compaction usage check. Zero/null are unknown, not an
                # overwrite. Context can decrease: never take a global max.
                if isinstance(context, dict) and not session_identity_uncertain:
                    context_size = context.get("contextWindow") or context_size
                yield context_info()
                break
        elif kind == "message_update":
            message = payload.get("message") or {}
            if not isinstance(message, dict):
                message = {}
            if message.get("role", "assistant") == "assistant":
                tokens = positive_tokens(message.get("usage")) or positive_tokens(
                    payload.get("usage")
                )
                if tokens is not None and not session_identity_uncertain:
                    context_used = tokens
                    provisional_usage = True
                    yield context_info()
            delta_event = payload.get("assistantMessageEvent") or {}
            delta_type = delta_event.get("type")
            if delta_type == "text_delta":
                piece = delta_event.get("delta") or ""
                if piece:
                    output_started = True
                text_parts.append(piece)
                yield {"type": "chunk", "text": piece}
            elif delta_type == "thinking_delta":
                piece = delta_event.get("delta") or ""
                if piece:
                    output_started = True
                    yield {"type": "thinking", "text": piece}
            elif delta_type in {"toolcall_start", "toolcall_delta", "toolcall_end"}:
                output_started = True
        elif kind == "tool_execution_start":
            name = payload.get("toolName") or "tool"
            args = payload.get("args") or {}
            tool_id = payload.get("toolCallId") or name
            prompt_accepted = True
            tool_ever_started = True
            active_tools.add(tool_id)
            phase = "tool_running"
            yield {
                "type": "tool_start",
                "id": tool_id,
                "name": name,
                "title": _tool_title(name, args),
                "args": args,
            }
        elif kind == "tool_execution_update":
            yield {
                "type": "tool_progress",
                "id": payload.get("toolCallId") or payload.get("toolName") or "tool",
                "name": payload.get("toolName") or "tool",
                "output": _result_text(payload.get("partialResult")),
            }
        elif kind == "tool_execution_end":
            name = payload.get("toolName") or "tool"
            result = payload.get("result") or {}
            output = _result_text(result)
            is_ok = payload.get("isError") is not True
            tool_id = payload.get("toolCallId") or name
            active_tools.discard(tool_id)
            last_model_progress = loop.time()
            if not active_tools:
                phase = "model_wait"
            yield {
                "type": "tool_end",
                "id": tool_id,
                "name": name,
                "ok": is_ok,
                "output": output,
                "diff": ToolDiff.from_result(name, result, is_ok),
            }
        elif kind == "message_end":
            # Pi reports provider failures (usage limits, transport errors) as a
            # completed assistant message with stopReason "error"/"aborted".
            # A later successful assistant message means a retry recovered.
            message = payload.get("message") or {}
            if message.get("role") == "assistant":
                stop_reason = message.get("stopReason")
                final_assistant_stop = stop_reason == "stop" and initial_input_started
                if stop_reason in {"error", "aborted"}:
                    if provisional_usage:
                        context_used = confirmed_context_used
                        provisional_usage = False
                        yield context_info()
                    error_message = (
                        str(message.get("errorMessage") or "").strip()
                        or f"Model request {stop_reason}"
                    )
                    yield {"type": "error", "text": error_message}
                else:
                    error_message = None
                    tokens = positive_tokens(message.get("usage"))
                    if tokens is not None and not session_identity_uncertain:
                        context_used = tokens
                        confirmed_context_used = tokens
                        yield context_info()
                    elif provisional_usage:
                        # An interim update is not a finalized measurement.
                        context_used = confirmed_context_used
                        yield context_info()
                    provisional_usage = False
        elif kind == "agent_settled" and not stats_requested:
            last_model_progress = loop.time()
            phase = "settling_stats"
            yield {"type": "settled"}
            if finish_event is None:
                await request_stats()

    if steering_task is not None:
        steering_task.cancel()
        await asyncio.gather(steering_task, return_exceptions=True)
    # Unsolicited rebind already cleared usage and stopped projection above.
    restore_pending_inputs()
    if owner is not None:
        _ACTIVE_INPUT_RESTORERS.pop(owner, None)
    if proc.stdin is not None:
        proc.stdin.close()
    try:
        await asyncio.wait_for(proc.wait(), timeout=5.0)
    except TimeoutError:
        proc.terminate()
        try:
            await asyncio.wait_for(proc.wait(), timeout=5.0)
        except TimeoutError:
            proc.kill()
            await proc.wait()
        ok = False
        fail_reason = "agent backend did not exit"
    if owner is not None:
        _ACTIVE_PROCESSES.pop(owner, None)
    error_text = await stderr_task
    otherwise_successful = ok and not fail_reason and error_message is None and proc.returncode == 0
    if otherwise_successful and not session_identity_uncertain:
        if not initial_input_started:
            fail_reason = "Pi RPC run ended without this prompt's user message start."
        elif not final_assistant_stop:
            fail_reason = "Pi RPC run ended without an authoritative final assistant stop."
    success = otherwise_successful and final_assistant_stop
    yield {
        "type": "done",
        "text": (
            _IDENTITY_FAILURE_TEXT
            if session_identity_uncertain
            else (
                "".join(text_parts).strip()
                if success
                else error_message
                or fail_reason
                or error_text
                or f"Backend exited with code {proc.returncode}"
            )
        ),
        "ok": success and not session_identity_uncertain,
        **(
            {"reason_code": "session_identity_uncertain"}
            if session_identity_uncertain
            else (
                {"reason_code": "current_prompt_input_missing"}
                if otherwise_successful and not initial_input_started
                else (
                    {"reason_code": "assistant_final_stop_missing"}
                    if otherwise_successful and not final_assistant_stop
                    else {}
                )
            )
        ),
    }
