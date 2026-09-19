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
import shutil
from collections.abc import AsyncIterator, Sequence
from pathlib import Path
from typing import Any

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


async def stream_agent_events(
    agent_bin: str,
    agent_args: Sequence[str],
    task: str,
    cwd: str,
    env_extra: dict[str, str] | None = None,
) -> AsyncIterator[dict[str, Any]]:
    """Run the backend and yield events. Always ends with a ``done`` event."""
    if shutil.which(agent_bin) is None and not Path(agent_bin).is_file():
        yield {"type": "done", "text": f"agent backend {agent_bin!r} not found", "ok": False}
        return

    rpc_args = rpc_args_for(agent_bin, agent_args)
    stdin_payload: bytes | None = None
    argv: list[str]
    if rpc_args is not None:
        argv = [agent_bin, *rpc_args]
        stdin_payload = (
            json.dumps({"type": "get_state"})
            + "\n"
            + json.dumps({"type": "prompt", "message": task})
            + "\n"
        ).encode()
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
            stderr=asyncio.subprocess.DEVNULL,
        )
    except OSError as exc:
        yield {"type": "done", "text": f"agent launch failed: {exc}", "ok": False}
        return

    assert proc.stdout is not None
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
        yield {"type": "done", "text": "".join(text_parts).strip(), "ok": code == 0}
        return

    # RPC mode: JSON lines with agent events. Keep stdin open after the turn
    # long enough to ask Pi for its authoritative current context estimate.
    model_name: str | None = None
    session_name: str | None = None
    context_used: int | None = None
    context_size: int | None = None
    stats_requested = False
    while True:
        try:
            line = await asyncio.wait_for(
                proc.stdout.readline(), timeout=5.0 if stats_requested else None
            )
        except TimeoutError:
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
        if kind == "response" and payload.get("success"):
            command = payload.get("command")
            data = payload.get("data") or {}
            if command == "get_state":
                model = data.get("model") or {}
                provider = model.get("provider")
                model_id = model.get("id") or model.get("name")
                model_name = (
                    f"{provider}/{model_id}" if provider and model_id else model_id or provider
                )
                session_name = data.get("sessionName")
                context_size = model.get("contextWindow")
                yield {
                    "type": "agent_info",
                    "model": model_name,
                    "session_name": session_name,
                    "context_used": context_used,
                    "context_size": context_size,
                }
            elif command == "get_session_stats":
                context = data.get("contextUsage") or {}
                context_used = context.get("tokens")
                context_size = context.get("contextWindow") or context_size
                yield {
                    "type": "agent_info",
                    "model": model_name,
                    "session_name": session_name,
                    "context_used": context_used,
                    "context_size": context_size,
                }
                break
        elif kind == "message_update":
            usage = payload.get("usage") or {}
            if usage.get("totalTokens") is not None:
                context_used = usage["totalTokens"]
                yield {
                    "type": "agent_info",
                    "model": model_name,
                    "session_name": session_name,
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
            }
        elif kind == "agent_settled" and not stats_requested:
            if proc.stdin is not None:
                stats_requested = True
                try:
                    proc.stdin.write((json.dumps({"type": "get_session_stats"}) + "\n").encode())
                    await proc.stdin.drain()
                except (BrokenPipeError, ConnectionResetError):
                    pass
            else:
                break

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
    yield {"type": "done", "text": "".join(text_parts).strip(), "ok": ok and not fail_reason}
