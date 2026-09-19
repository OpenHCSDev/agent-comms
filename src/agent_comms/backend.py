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
        stdin_payload = (json.dumps({"type": "prompt", "message": task}) + "\n").encode()
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
        proc.stdin.write(stdin_payload)
        await proc.stdin.drain()
        proc.stdin.close()

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

    # RPC mode: JSON lines with agent events.
    async for line in proc.stdout:
        line = line.strip()
        if not line:
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        kind = payload.get("type")
        if kind == "message_update":
            delta_event = payload.get("assistantMessageEvent") or {}
            if delta_event.get("type") == "text_delta":
                piece = delta_event.get("delta") or ""
                text_parts.append(piece)
                yield {"type": "chunk", "text": piece}
        elif kind == "tool_execution_start":
            name = payload.get("toolName") or "tool"
            detail = _short_args(payload.get("args") or "")
            tool_id = payload.get("toolCallId") or name
            yield {"type": "tool_start", "id": tool_id, "name": name, "title": f"{name}: {detail}"}
        elif kind == "tool_execution_end":
            name = payload.get("toolName") or "tool"
            result = payload.get("result") or {}
            output = _short_args(
                "".join(
                    block.get("text", "")
                    for block in (result.get("content") or [])
                    if isinstance(block, dict)
                ),
                200,
            )
            is_ok = payload.get("isError") is not True
            if not is_ok:
                ok = False
                fail_reason = f"tool {name} failed: {output}"
            yield {
                "type": "tool_end",
                "id": payload.get("toolCallId") or name,
                "name": name,
                "ok": is_ok,
                "output": output,
            }
        elif kind == "agent_end":
            break

    await proc.wait()
    yield {"type": "done", "text": "".join(text_parts).strip(), "ok": ok and not fail_reason}
