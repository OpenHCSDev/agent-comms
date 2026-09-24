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
    {"type": "done",       "text": str, "ok": bool}

The runner never raises on backend failure; it yields ``done`` with
``ok=False`` and the error as text. Callers own presentation.
"""

from __future__ import annotations

import asyncio
import json
import os
import secrets
import shutil
import signal
from collections.abc import AsyncIterator, Sequence
from contextlib import suppress
from pathlib import Path
from typing import Any
from uuid import uuid4

from .native_pi import CAPABILITY as NATIVE_INPUT_CAPABILITY
from .tool_results import ToolDiff

RPC_FLAG = "--mode"
RPC_VALUE = "rpc"
PROMPT_START_TIMEOUT_SECONDS = 180.0
CAPABILITY_PREFLIGHT_TIMEOUT_SECONDS = 5.0
_TOOL_KINDS = {
    "bash": "execute",
    "read": "read",
    "write": "edit",
    "edit": "edit",
    "grep": "search",
    "glob": "search",
}
_ACTIVE_PROCESSES: dict[asyncio.Task[Any], asyncio.subprocess.Process] = {}
_ACTIVE_STDERR_TASKS: dict[asyncio.Task[Any], asyncio.Task[str]] = {}
_ACTIVE_STEERING_TASKS: dict[asyncio.Task[Any], asyncio.Task[None]] = {}


def _close_child_stdin(proc: asyncio.subprocess.Process) -> None:
    """A broken closing pipe must not prevent terminating an uncertain turn."""
    if proc.stdin is not None:
        # A child that exited mid-preflight can close its read end first.
        # Continue the process-group teardown and wait for its exit.
        with suppress(OSError, RuntimeError, ValueError):
            proc.stdin.close()


async def _stop_task_steering(task: asyncio.Task[Any]) -> None:
    """Reap a queue-forwarder even if the RPC parser or its consumer exits early."""
    forwarder = _ACTIVE_STEERING_TASKS.pop(task, None)
    if forwarder is not None:
        forwarder.cancel()
        await asyncio.gather(forwarder, return_exceptions=True)


async def terminate_task_process(task: asyncio.Task[Any]) -> None:
    """Terminate the backend subprocess owned by an agent turn task."""
    proc = _ACTIVE_PROCESSES.pop(task, None)
    if proc is None:
        return
    _close_child_stdin(proc)
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


def rpc_args_for(bin_name: str, args: Sequence[str]) -> list[str] | None:
    """RPC-mode args for a backend, or None if the backend should run in text mode.

    pi gets ``--mode rpc`` inserted; the prompt arrives on stdin as a JSON
    ``{"type": "prompt", "message": ...}`` line.
    """
    if Path(bin_name).name.startswith("pi") or bin_name.endswith("/pi"):
        return [*args, RPC_FLAG, RPC_VALUE]
    return None


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


async def _stream_agent_events_impl(
    agent_bin: str,
    agent_args: Sequence[str],
    task: str,
    cwd: str,
    env_extra: dict[str, str] | None = None,
    session_file: str | None = None,
    steering_queue: asyncio.Queue[str] | None = None,
    finish_event: asyncio.Event | None = None,
    fork_session: bool = False,
    require_input_id: bool = True,
) -> AsyncIterator[dict[str, Any]]:
    """Run the backend and yield events. Always ends with a ``done`` event.

    Pi RPC requires native per-input IDs by default: stock Pi's untagged
    message_start cannot prove which accepted command actually began. The
    opt-out is for isolated legacy fixture tests, never automatic fallback.
    """
    if shutil.which(agent_bin) is None and not Path(agent_bin).is_file():
        yield {"type": "done", "text": f"agent backend {agent_bin!r} not found", "ok": False}
        return

    rpc_args = rpc_args_for(agent_bin, agent_args)
    stdin_payload: bytes | None = None
    argv: list[str]
    # One initial prompt per fresh RPC subprocess; the ID distinguishes its
    # ACK from auxiliary get_state/stats and forwarded input responses.
    prompt_id = "agent-comms-prompt"
    preflight_id = f"agent-comms-preflight-{secrets.token_hex(16)}"
    original_input_id = secrets.token_hex(16)
    if rpc_args is not None:
        argv = [agent_bin, *rpc_args]
        if session_file:
            argv += ["--fork" if fork_session else "--session", session_file]
        prompt_payload = (
            json.dumps(
                {"type": "prompt", "id": prompt_id, "inputId": original_input_id, "message": task}
            )
            + "\n"
        ).encode()
        # A stock Pi may silently ignore inputId. In the strict/default path,
        # do not send a potentially paid prompt until get_state advertises the
        # reviewed native input-ID capability. Never fall back to text matching.
        stdin_payload = (json.dumps({"type": "get_state", "id": preflight_id}) + "\n").encode()
        if not require_input_id:
            stdin_payload += prompt_payload
    else:
        argv = [agent_bin, *agent_args, task]

    env = os.environ.copy()
    if env_extra:
        env.update(env_extra)
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
    if owner is not None:
        _ACTIVE_STDERR_TASKS[owner] = stderr_task
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

    # Native inputId is the default authority. The legacy fixture opt-out
    # retains a text marker only for historical parsing tests; it is not a
    # production provenance mechanism and must not appear in native prompts.
    forwarded_inputs: dict[str, tuple[str, str]] = {}
    accepted_inputs: dict[str, tuple[str, str]] = {}
    rejected_input = False
    steering_task: asyncio.Task[None] | None = None

    async def forward_steering() -> None:
        assert steering_queue is not None and proc.stdin is not None
        while True:
            message = await steering_queue.get()
            input_id = f"agent-comms-steer-{uuid4().hex}"
            marked_message = (
                message if require_input_id else f"[agent-comms input-id: {input_id}]\n{message}"
            )
            native_input_id = secrets.token_hex(16)
            forwarded_inputs[input_id] = (marked_message, native_input_id)
            try:
                proc.stdin.write(
                    (
                        json.dumps(
                            {
                                "type": "prompt",
                                "id": input_id,
                                "inputId": native_input_id,
                                "message": marked_message,
                                "streamingBehavior": "steer",
                            }
                        )
                        + "\n"
                    ).encode()
                )
                await proc.stdin.drain()
            except (BrokenPipeError, ConnectionResetError):
                # ACP may already have advanced its inbox cursor. Retain this
                # input as unresolved rather than reporting the original-only
                # assistant reply as a completed turn.
                return

    # RPC mode: JSON lines with agent events. Keep stdin open after the turn
    # long enough to ask Pi for its authoritative current context estimate.
    model_name: str | None = None
    session_name: str | None = None
    active_session_file = session_file
    context_used: int | None = None
    context_size: int | None = None
    stats_requested = False
    prompt_acknowledged = False
    native_capability_confirmed = not require_input_id
    original_input_started = False
    input_uncertain = False
    final_assistant_stop = False
    abort_for_missing_input = False
    capability_failed = False
    prompt_start_deadline = asyncio.get_running_loop().time() + (
        CAPABILITY_PREFLIGHT_TIMEOUT_SECONDS if require_input_id else PROMPT_START_TIMEOUT_SECONDS
    )

    async def request_stats() -> None:
        nonlocal stats_requested
        if proc.stdin is None or stats_requested:
            return
        stats_requested = True
        try:
            proc.stdin.write(
                (json.dumps({"type": "get_state", "id": "postturn-state"}) + "\n").encode()
            )
            proc.stdin.write((json.dumps({"type": "get_session_stats"}) + "\n").encode())
            await proc.stdin.drain()
        except (BrokenPipeError, ConnectionResetError):
            pass

    while True:
        start_wait = (
            max(0.0, prompt_start_deadline - asyncio.get_running_loop().time())
            if not original_input_started
            else None
        )
        if start_wait == 0.0:
            capability_failed = require_input_id and not native_capability_confirmed
            fail_reason = (
                "Pi native input-ID capability preflight timed out."
                if capability_failed
                else "Pi RPC run ended without this prompt's user message start."
            )
            abort_for_missing_input = True
            ok = False
            break
        try:
            if finish_event is not None and not stats_requested:
                read_task = asyncio.create_task(proc.stdout.readline())
                finish_task = asyncio.create_task(finish_event.wait())
                done, _ = await asyncio.wait(
                    {read_task, finish_task},
                    timeout=start_wait,
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if not done:
                    read_task.cancel()
                    finish_task.cancel()
                    await asyncio.gather(read_task, finish_task, return_exceptions=True)
                    raise TimeoutError
                if read_task in done:
                    # If both complete together, never discard the Pi record:
                    # it may be an untracked user start that revokes proof.
                    finish_task.cancel()
                    await asyncio.gather(finish_task, return_exceptions=True)
                    line = read_task.result()
                else:
                    read_task.cancel()
                    await asyncio.gather(read_task, return_exceptions=True)
                    if require_input_id and not native_capability_confirmed:
                        fail_reason = "Pi native input-ID capability preflight was interrupted."
                        capability_failed = True
                        abort_for_missing_input = True
                        ok = False
                        break
                    await request_stats()
                    continue
            else:
                stats_wait = 5.0 if stats_requested else None
                timeout = (
                    min(stats_wait, start_wait)
                    if stats_wait is not None and start_wait is not None
                    else stats_wait if start_wait is None else start_wait
                )
                line = await asyncio.wait_for(proc.stdout.readline(), timeout=timeout)
        except TimeoutError:
            if not original_input_started:
                capability_failed = require_input_id and not native_capability_confirmed
                fail_reason = (
                    "Pi native input-ID capability preflight timed out."
                    if capability_failed
                    else "Pi RPC run ended without this prompt's user message start."
                )
                abort_for_missing_input = True
                ok = False
            break
        if not line:
            if require_input_id and not native_capability_confirmed:
                fail_reason = "Pi native input-ID capability preflight ended before attestation."
                capability_failed = True
                ok = False
            break
        line = line.strip()
        if not line:
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(payload, dict):
            continue
        kind = payload.get("type")
        if (
            require_input_id
            and not native_capability_confirmed
            and kind == "response"
            and payload.get("command") == "get_state"
            and (payload.get("id") != preflight_id or payload.get("success") is not True)
        ):
            fail_reason = "Pi native input-ID capability preflight failed."
            capability_failed = True
            abort_for_missing_input = True
            ok = False
            break
        if kind == "response" and payload.get("command") == "prompt":
            input_id = payload.get("id")
            if input_id == prompt_id:
                prompt_acknowledged = payload.get("success") is True
            elif isinstance(input_id, str) and input_id in forwarded_inputs:
                forwarded = forwarded_inputs.pop(input_id)
                if payload.get("success") is True:
                    accepted_inputs[input_id] = forwarded
                else:
                    # Rejection also leaves an ACP-enqueued input undelivered;
                    # a different assistant's final cannot settle that input.
                    rejected_input = True
        if kind == "response" and payload.get("success"):
            command = payload.get("command")
            data = payload.get("data")
            if not isinstance(data, dict):
                if command == "get_state" and require_input_id and not native_capability_confirmed:
                    fail_reason = "Pi native input-ID capability preflight had invalid state data."
                    capability_failed = True
                    abort_for_missing_input = True
                    ok = False
                    break
                data = {}
            if command == "get_state":
                if not native_capability_confirmed:
                    if (
                        payload.get("id") != preflight_id
                        or data.get("nativeInputProofCapability") != NATIVE_INPUT_CAPABILITY
                    ):
                        fail_reason = "Pi backend lacks the required native input-ID capability."
                        capability_failed = True
                        abort_for_missing_input = True
                        ok = False
                        break
                    native_capability_confirmed = True
                    prompt_start_deadline = (
                        asyncio.get_running_loop().time() + PROMPT_START_TIMEOUT_SECONDS
                    )
                    assert proc.stdin is not None
                    try:
                        proc.stdin.write(prompt_payload)
                        await proc.stdin.drain()
                    except (BrokenPipeError, ConnectionResetError):
                        fail_reason = "Pi RPC prompt could not be sent after capability preflight."
                        ok = False
                        break
                model = data.get("model")
                if not isinstance(model, dict):
                    model = {}
                provider = model.get("provider")
                model_id = model.get("id") or model.get("name")
                model_name = (
                    f"{provider}/{model_id}" if provider and model_id else model_id or provider
                )
                session_name = data.get("sessionName")
                active_session_file = data.get("sessionFile") or active_session_file
                context_size = model.get("contextWindow")
                yield {
                    "type": "agent_info",
                    "model": model_name,
                    "session_name": session_name,
                    "session_file": active_session_file,
                    "context_used": context_used,
                    "context_size": context_size,
                }
            elif command == "get_session_stats":
                context = data.get("contextUsage")
                if not isinstance(context, dict):
                    context = {}
                context_used = context.get("tokens")
                context_size = context.get("contextWindow") or context_size
                yield {
                    "type": "agent_info",
                    "model": model_name,
                    "session_name": session_name,
                    "session_file": active_session_file,
                    "context_used": context_used,
                    "context_size": context_size,
                }
                break
        elif kind == "message_start":
            message = payload.get("message")
            if not isinstance(message, dict):
                message = {}
            if message.get("role") == "user":
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
                native_id = message.get("inputId")
                if (
                    prompt_acknowledged
                    and native_capability_confirmed
                    and not original_input_started
                    and not input_uncertain
                    and user_text == task
                    and (not require_input_id or native_id == original_input_id)
                ):
                    original_input_started = True
                    # Prequeued ACP messages must not race ahead of this
                    # prompt's first user start into Pi's idle preflight.
                    if steering_queue is not None and proc.stdin is not None:
                        steering_task = asyncio.create_task(forward_steering())
                        if owner is not None:
                            _ACTIVE_STEERING_TASKS[owner] = steering_task
                else:
                    matches = [
                        input_id
                        for input_id, (marked_message, accepted_native_id) in (
                            accepted_inputs.items()
                        )
                        if user_text is not None
                        and user_text == marked_message
                        and (not require_input_id or native_id == accepted_native_id)
                    ]
                    if original_input_started and not input_uncertain and len(matches) == 1:
                        accepted_inputs.pop(matches[0])
                    else:
                        # ACK/text alone is not proof; missing/wrong native ID,
                        # duplicate input, or a second original start fails closed.
                        input_uncertain = True
                        original_input_started = False
                        final_assistant_stop = False
                        fail_reason = "Pi RPC run ended without this prompt's user message start."
                        abort_for_missing_input = True
                        ok = False
                        break
            elif message.get("role") == "assistant":
                final_assistant_stop = False
        elif kind == "message_end":
            message = payload.get("message")
            if not isinstance(message, dict):
                message = {}
            if message.get("role") == "assistant":
                final_assistant_stop = (
                    message.get("stopReason") == "stop"
                    and original_input_started
                    and not input_uncertain
                )
        elif kind == "compaction_start":
            context_used = None
            reason = payload.get("reason")
            yield {
                "type": "compaction_start",
                "reason": reason if reason in {"manual", "threshold", "overflow"} else "unknown",
            }
        elif kind == "compaction_end":
            context_used = None
            result = payload.get("result")
            completed = payload.get("aborted") is False and isinstance(result, dict)
            summary = result.get("summary") if completed and isinstance(result, dict) else None
            if payload.get("willRetry") is True:
                final_assistant_stop = False
            reason = payload.get("reason")
            yield {
                "type": "compaction_end",
                "reason": reason if reason in {"manual", "threshold", "overflow"} else "unknown",
                "aborted": not completed,
                "summary": summary.strip()[:4096] if isinstance(summary, str) else None,
                "context_used": None,
                "will_retry": payload.get("willRetry") is True,
            }
        elif (
            kind == "agent_start"
            or (kind == "agent_end" and payload.get("willRetry") is True)
            or kind == "auto_retry_start"
        ):
            # A prior assistant stop cannot complete a later run/retry which
            # exits or settles without its own authoritative final message.
            final_assistant_stop = False
        elif kind == "message_update":
            usage = payload.get("usage") or {}
            if usage.get("totalTokens") is not None:
                context_used = usage["totalTokens"]
                yield {
                    "type": "agent_info",
                    "model": model_name,
                    "session_name": session_name,
                    "session_file": active_session_file,
                    "context_used": context_used,
                    "context_size": context_size,
                }
            delta_event = payload.get("assistantMessageEvent") or {}
            delta_type = delta_event.get("type")
            if delta_type == "text_delta":
                piece = delta_event.get("delta") or ""
                text_parts.append(piece)
                yield {"type": "chunk", "text": piece}
            elif delta_type == "thinking_delta":
                piece = delta_event.get("delta") or ""
                if piece:
                    yield {"type": "thinking", "text": piece}
        elif kind == "tool_execution_start":
            name = payload.get("toolName") or "tool"
            args = payload.get("args") or {}
            tool_id = payload.get("toolCallId") or name
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
            yield {
                "type": "tool_end",
                "id": payload.get("toolCallId") or name,
                "name": name,
                "ok": is_ok,
                "output": output,
                "diff": ToolDiff.from_result(name, result, is_ok),
            }
        elif kind == "agent_settled" and not stats_requested:
            yield {"type": "settled"}
            if finish_event is None:
                await request_stats()

    if owner is not None:
        await _stop_task_steering(owner)
    elif steering_task is not None:
        steering_task.cancel()
        await asyncio.gather(steering_task, return_exceptions=True)
    if abort_for_missing_input and owner is not None:
        # The RPC process can stay alive after an extension handles a prompt
        # without starting a model run. Stop its whole process group; never
        # issue the uncertain prompt again.
        await terminate_task_process(owner)
    _close_child_stdin(proc)
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
    otherwise_successful = ok and not fail_reason and proc.returncode == 0
    queued_input_uncertain = otherwise_successful and (
        rejected_input
        or bool(forwarded_inputs)
        or bool(accepted_inputs)
        or (steering_queue is not None and not steering_queue.empty())
    )
    if otherwise_successful and not original_input_started:
        fail_reason = "Pi RPC run ended without this prompt's user message start."
    elif otherwise_successful and not final_assistant_stop:
        fail_reason = "Pi RPC run ended without an authoritative final assistant stop."
    elif queued_input_uncertain:
        fail_reason = "Pi RPC run ended with an unstarted queued input; delivery is uncertain."
    success = (
        otherwise_successful
        and original_input_started
        and final_assistant_stop
        and not queued_input_uncertain
    )
    yield {
        "type": "done",
        "text": (
            "".join(text_parts).strip()
            if success
            else fail_reason or error_text or f"Backend exited with code {proc.returncode}"
        ),
        "ok": success,
        **(
            {"reason_code": "pi_input_id_unavailable"}
            if capability_failed
            else (
                {"reason_code": "current_prompt_input_missing"}
                if (otherwise_successful or abort_for_missing_input) and not original_input_started
                else (
                    {"reason_code": "assistant_final_stop_missing"}
                    if otherwise_successful and not final_assistant_stop
                    else (
                        {"reason_code": "queued_input_start_missing"}
                        if queued_input_uncertain
                        else {}
                    )
                )
            )
        ),
    }


async def stream_agent_events(
    agent_bin: str,
    agent_args: Sequence[str],
    task: str,
    cwd: str,
    env_extra: dict[str, str] | None = None,
    session_file: str | None = None,
    steering_queue: asyncio.Queue[str] | None = None,
    finish_event: asyncio.Event | None = None,
    fork_session: bool = False,
    require_input_id: bool = True,
) -> AsyncIterator[dict[str, Any]]:
    """Fail closed with one terminal result even on malformed Pi RPC records.

    The inner parser may encounter an unexpected JSON shape. Its ordinary
    cleanup is then bypassed, so this outer boundary always tears down the
    registered child before returning a fixed, non-sensitive failure. Never
    replay an input whose provider opportunity is uncertain.
    """
    owner = asyncio.current_task()
    terminal_seen = False
    try:
        async for event in _stream_agent_events_impl(
            agent_bin,
            agent_args,
            task,
            cwd,
            env_extra,
            session_file,
            steering_queue,
            finish_event,
            fork_session,
            require_input_id,
        ):
            if event.get("type") == "done":
                terminal_seen = True
            yield event
    except Exception:
        if owner is not None:
            await _stop_task_steering(owner)
            await terminate_task_process(owner)
            stderr_task = _ACTIVE_STDERR_TASKS.pop(owner, None)
            if stderr_task is not None:
                if not stderr_task.done():
                    stderr_task.cancel()
                await asyncio.gather(stderr_task, return_exceptions=True)
        if not terminal_seen:
            yield {
                "type": "done",
                "ok": False,
                "reason_code": "pi_invalid_rpc_event",
                "text": "Pi RPC returned an invalid event; this turn was not completed.",
            }
    finally:
        if owner is not None:
            await _stop_task_steering(owner)
            await terminate_task_process(owner)
            stderr_task = _ACTIVE_STDERR_TASKS.pop(owner, None)
            if stderr_task is not None:
                if not stderr_task.done():
                    stderr_task.cancel()
                await asyncio.gather(stderr_task, return_exceptions=True)
