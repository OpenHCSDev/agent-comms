"""Explicit, one-shot Pi RPC compaction; never a prompt or automatic retry.

The response describes Pi's completed command, not a signed provider invoice or a
fresh context-usage measurement. Callers must keep usage unknown until a new
session-stats response is observed. This module is inert until explicitly called.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import signal
import unicodedata
from collections.abc import Sequence
from contextlib import suppress
from pathlib import Path
from typing import Any
from uuid import uuid4

from .backend import rpc_args_for

MAX_LINE_BYTES = 64 * 1024
MAX_OUTPUT_BYTES = 256 * 1024
MAX_INSTRUCTIONS_CHARS = 4096
MAX_SUMMARY_CHARS = 1000
DEFAULT_TIMEOUT_SECONDS = 300.0
EXIT_GRACE_SECONDS = 1.0
# Pi's --no-session takes precedence over a later --session; the other
# selectors can likewise make a successful RPC compact the wrong session.
_SESSION_OVERRIDE_FLAGS = frozenset(
    {
        "--no-session",
        "--session",
        "--session-id",
        "--session-dir",
        "--fork",
        "--continue",
        "-c",
        "--resume",
        "-r",
    }
)


def _safe_summary(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    safe = "".join(" " if unicodedata.category(char).startswith("C") else char for char in value)
    return " ".join(safe.split())[:MAX_SUMMARY_CHARS]


def _nonnegative_count(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def _group_alive(pgid: int) -> bool:
    if os.name != "posix":
        return False
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # Unknown ownership is not proof of a clean group exit.
        return True
    return True


def _signal_process_group(proc: asyncio.subprocess.Process, *, kill: bool = False) -> None:
    try:
        if os.name == "posix":
            # A Pi process can exit while a child still holds stdout open. Its
            # original PGID stays allocated for as long as that group exists.
            os.killpg(proc.pid, signal.SIGKILL if kill else signal.SIGTERM)
        elif proc.returncode is None:
            if kill:
                proc.kill()
            else:
                proc.terminate()
    except (ProcessLookupError, PermissionError):
        pass


async def _wait_group_exit(pgid: int) -> bool:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + EXIT_GRACE_SECONDS
    while _group_alive(pgid):
        if loop.time() >= deadline:
            return False
        await asyncio.sleep(0.02)
    return True


async def _close_and_reap(proc: asyncio.subprocess.Process, *, force: bool) -> bool:
    """Bound shutdown of both Pi and its process group, including orphan children."""
    if proc.stdin is not None and not proc.stdin.is_closing():
        with suppress(OSError):
            proc.stdin.close()
    if force:
        _signal_process_group(proc)
    try:
        await asyncio.wait_for(proc.wait(), timeout=EXIT_GRACE_SECONDS)
    except TimeoutError:
        force = True
        _signal_process_group(proc, kill=True)
        try:
            await asyncio.wait_for(proc.wait(), timeout=EXIT_GRACE_SECONDS)
        except TimeoutError:
            return False
    if os.name == "posix" and _group_alive(proc.pid):
        # Exiting the direct Pi PID is not enough: an inherited pipe or a
        # lingering extension subprocess can keep this session group alive.
        force = True
        _signal_process_group(proc)
        if not await _wait_group_exit(proc.pid):
            _signal_process_group(proc, kill=True)
            if not await _wait_group_exit(proc.pid):
                return False
    return not force and proc.returncode == 0


async def compact_session(
    agent_bin: str,
    agent_args: Sequence[str],
    session_file: str,
    cwd: str,
    custom_instructions: str | None = None,
    *,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    """Run one explicitly requested manual compaction against a saved session.

    All negative outcomes have stable public diagnostics. Raw Pi stderr, RPC
    errors, arbitrary result details, and paths are never returned to the UI.
    Cancellation propagates only after the child process has been stopped and
    reaped; it never replays an uncertain command.
    """
    rpc_args = rpc_args_for(agent_bin, agent_args)
    if rpc_args is None:
        return {"ok": False, "error": "This backend does not support context compaction."}
    if any(
        not isinstance(arg, str)
        or arg == "--"  # Pi stops parsing; appended --mode/--session become positional prompts.
        or arg in _SESSION_OVERRIDE_FLAGS
        or any(
            arg.startswith(f"{flag}=") for flag in _SESSION_OVERRIDE_FLAGS if flag.startswith("--")
        )
        for arg in agent_args
    ):
        return {"ok": False, "error": "Compaction arguments may not change the saved session."}
    if not session_file or not Path(session_file).is_file():
        return {"ok": False, "error": "This thread has no persisted session to compact."}
    if not Path(cwd).is_dir():
        return {"ok": False, "error": "This thread's project directory is unavailable."}
    if not (shutil.which(agent_bin) or Path(agent_bin).is_file()):
        return {"ok": False, "error": "Compaction backend is unavailable."}
    if custom_instructions is not None and (
        not isinstance(custom_instructions, str)
        or len(custom_instructions) > MAX_INSTRUCTIONS_CHARS
    ):
        return {"ok": False, "error": "Compaction instructions are invalid or too long."}
    if not 0 < timeout_seconds <= DEFAULT_TIMEOUT_SECONDS:
        return {"ok": False, "error": "Compaction timeout is invalid."}

    env = os.environ.copy()
    if env.get("AGENT_COMMS_MANAGED") == "1":
        env["PI_WORKTREE"] = str(Path(cwd).resolve())
    command_id = f"agent-comms-compact-{uuid4().hex}"
    command: dict[str, Any] = {"id": command_id, "type": "compact"}
    if custom_instructions and custom_instructions.strip():
        command["customInstructions"] = custom_instructions
    try:
        proc = await asyncio.create_subprocess_exec(
            agent_bin,
            *rpc_args,
            "--session",
            str(Path(session_file).resolve()),
            cwd=cwd,
            env=env,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            limit=MAX_LINE_BYTES,
            start_new_session=os.name == "posix",
        )
    except OSError:
        return {"ok": False, "error": "Compaction backend could not be started."}

    assert proc.stdin is not None and proc.stdout is not None and proc.stderr is not None
    stderr_reader = proc.stderr

    async def discard_stderr() -> None:
        while await stderr_reader.read(4096):
            pass

    stderr_task = asyncio.create_task(discard_stderr())
    result: dict[str, Any] = {"ok": False, "error": "Compaction ended without a matching response."}
    force = False
    cancelled = False
    try:
        async with asyncio.timeout(timeout_seconds):
            proc.stdin.write((json.dumps(command) + "\n").encode())
            await proc.stdin.drain()
            read_bytes = 0
            while True:
                line = await proc.stdout.readline()
                if not line:
                    break
                read_bytes += len(line)
                if read_bytes > MAX_OUTPUT_BYTES:
                    result = {
                        "ok": False,
                        "error": "Compaction response exceeded its output limit.",
                    }
                    force = True
                    break
                try:
                    payload = json.loads(line)
                except (UnicodeDecodeError, json.JSONDecodeError):
                    result = {"ok": False, "error": "Compaction returned an invalid response."}
                    force = True
                    break
                if not isinstance(payload, dict):
                    result = {"ok": False, "error": "Compaction returned an invalid response."}
                    force = True
                    break
                if payload.get("type") != "response" or payload.get("id") != command_id:
                    continue
                if payload.get("command") != "compact" or type(payload.get("success")) is not bool:
                    result = {"ok": False, "error": "Compaction returned an invalid response."}
                    force = True
                    break
                if payload["success"] is not True:
                    result = {"ok": False, "error": "Compaction failed; inspect local diagnostics."}
                    break
                data = payload.get("data")
                if not isinstance(data, dict):
                    result = {"ok": False, "error": "Compaction returned an invalid response."}
                    force = True
                    break
                result = {"ok": True, "summary": _safe_summary(data.get("summary"))}
                before = _nonnegative_count(data.get("tokensBefore"))
                after = _nonnegative_count(data.get("estimatedTokensAfter"))
                if before is not None:
                    result["tokensBefore"] = before
                if after is not None:
                    result["estimatedTokensAfter"] = after
                # Neither count is fresh measured context usage.
                break
    except TimeoutError:
        result = {"ok": False, "error": "Compaction timed out."}
        force = True
    except (ValueError, OSError, BrokenPipeError, ConnectionResetError):
        result = {"ok": False, "error": "Compaction transport failed."}
        force = True
    except asyncio.CancelledError:
        cancelled = True
        force = True
    finally:
        cleanup = asyncio.create_task(_close_and_reap(proc, force=force))
        while not cleanup.done():
            try:
                await asyncio.shield(cleanup)
            except asyncio.CancelledError:
                cancelled = True
        exited_cleanly = cleanup.result()
        if not stderr_task.done():
            stderr_task.cancel()
        await asyncio.gather(stderr_task, return_exceptions=True)
    if cancelled:
        raise asyncio.CancelledError
    if not exited_cleanly and result.get("ok"):
        return {"ok": False, "error": "Compaction did not finish cleanly."}
    return result
