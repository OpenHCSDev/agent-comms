"""Agent communications — ACP server on the official protocol library.

Implements an Agent Client Protocol agent so ACP clients (Toad, Zed, VS Code)
attach to the coordination wire natively: each client session is one thread
on the wire, prompts are messages on the bus, and replies come back as
``agent_message_chunk`` session updates.

Two turn modes:

- **relay** (default): the prompt is broadcast on the wire and the thread's
  inbox drains back as agent messages. You are talking to the *room*.
- **agent**: the prompt also goes to a real coding agent (pi, headless
  ``--print`` mode) whose streamed reply is forwarded to the client and
  posted to the thread's channel, so other threads can read it.

Turn mode is chosen per prompt: a leading ``!agent `` selects the agent
turn; everything else relays. This keeps the wire semantics in the core and
lets one Toad session act as both a chat participant and a working agent.
"""

from __future__ import annotations

import asyncio
import os
import re
import shutil
from pathlib import Path
from typing import Any

from acp import RequestError, run_agent
from acp.schema import (
    AgentCapabilities,
    AgentMessageChunk,
    Implementation,
    InitializeResponse,
    NewSessionResponse,
    PromptResponse,
    TextContentBlock,
)

from .operations import Comms, wire

GLOBAL_TARGET = "#all"
AGENT_PREFIX = "!agent "
DEFAULT_AGENT_BIN = "pi"
DEFAULT_AGENT_ARGS = [
    "--print",
    "--no-session",
    "--provider",
    "openrouter",
    "--model",
    "z-ai/glm-5.3-flash",
]
LIVE_DRAIN_INTERVAL = 1.0


class CommsAgent:
    """ACP agent bound to one Comms wire.

    Session -> thread. A prompt broadcasts the text to the session's thread
    on the global channel, drains the thread's inbox back to the client as
    agent messages, then acknowledges.
    """

    def __init__(
        self, comms: Comms, agent_bin: str | None = None, agent_args: list[str] | None = None
    ):
        self._comms = comms
        self._sessions: dict[str, str] = {}
        self._next = 0
        self._client: Any = None
        self._agent_bin = agent_bin or os.environ.get("AGENT_COMMS_AGENT_BIN", DEFAULT_AGENT_BIN)
        arg_env = os.environ.get("AGENT_COMMS_AGENT_ARGS", "")
        self._agent_args = (
            agent_args
            if agent_args is not None
            else (arg_env.split() if arg_env else list(DEFAULT_AGENT_ARGS))
        )
        self._drain_tasks: dict[str, asyncio.Task[None]] = {}

    def on_connect(self, client: Any) -> None:
        """Called by AgentSideConnection with the client-facing connection."""
        self._client = client

    # ─── ACP methods ─────────────────────────────────────────────────────────

    async def initialize(
        self,
        protocol_version: int,
        client_capabilities: Any = None,
        client_info: Any = None,
    ) -> InitializeResponse:
        return InitializeResponse(
            protocol_version=protocol_version,
            agent_capabilities=AgentCapabilities(load_session=False),
            agent_info=Implementation(name="agent-comms", title="Agent Comms", version="0.1.0"),
            auth_methods=[],
        )

    async def new_session(
        self, cwd: str, mcp_servers: list[Any] | None = None, **kwargs: Any
    ) -> NewSessionResponse:
        thread_name = self._ensure_thread(self._thread_name_for(cwd), cwd)
        self._next += 1
        session_id = f"s{self._next}"
        self._sessions[session_id] = thread_name
        self._ensure_live_drain(session_id, thread_name)
        return NewSessionResponse(session_id=session_id)

    async def prompt(self, session_id: str, prompt: list[Any], **kwargs: Any) -> PromptResponse:
        thread_name = self._require_session(session_id)
        self._comms.registry.require(thread_name)
        text = self._prompt_text(prompt)
        agent_task: str | None = None
        if text.startswith(AGENT_PREFIX):
            agent_task = text[len(AGENT_PREFIX) :].strip()
        if text:
            self._comms.send(thread_name, GLOBAL_TARGET, text)
        await self._drain_inbox(session_id, thread_name)
        if agent_task:
            await self._run_agent_turn(session_id, thread_name, agent_task)
        self._ensure_live_drain(session_id, thread_name)
        return PromptResponse(stop_reason="end_turn")

    async def cancel(self, session_id: str, **kwargs: Any) -> None:
        task = self._drain_tasks.pop(session_id, None)
        if task is not None:
            task.cancel()
        thread_name = self._sessions.get(session_id)
        if thread_name:
            self._comms.acknowledge(thread_name)

    async def authenticate(self, method_id: str, **kwargs: Any) -> None:
        raise RequestError.auth_required({"reason": "agent-comms requires no authentication"})

    # ─── Helpers ─────────────────────────────────────────────────────────────

    def _require_session(self, session_id: str) -> str:
        thread_name = self._sessions.get(session_id)
        if thread_name is None:
            raise RequestError.invalid_params({"reason": f"Unknown sessionId: {session_id!r}"})
        return thread_name

    @staticmethod
    def _thread_name_for(cwd: str) -> str:
        leaf = re.sub(r"[^A-Za-z0-9_-]+", "-", Path(cwd).name or "session").strip("-")
        return leaf or "session"

    def _ensure_thread(self, name: str, cwd: str) -> str:
        """Register (or reuse) the thread for this session.

        Same name with a different worktree gets folder-disambiguated so a
        thread's folder can never be silently re-pointed.
        """
        from .declarations import Thread

        if name in self._comms.registry:
            existing = self._comms.registry.require(name)
            if existing.worktree == cwd:
                return name
            suffix = 2
            while f"{name}-{suffix}" in self._comms.registry:
                suffix += 1
            name = f"{name}-{suffix}"
        thread = Thread(name=name, tags=frozenset({"acp"}), worktree=cwd)
        self._comms.register(thread)
        return name

    def _ensure_live_drain(self, session_id: str, thread_name: str) -> None:
        """Keep one background task per session pushing inbox messages live."""
        if session_id in self._drain_tasks and not self._drain_tasks[session_id].done():
            return

        async def loop() -> None:
            while True:
                await asyncio.sleep(LIVE_DRAIN_INTERVAL)
                await self._drain_inbox(session_id, thread_name)

        self._drain_tasks[session_id] = asyncio.create_task(loop())

    async def _drain_inbox(self, session_id: str, thread_name: str) -> None:
        from .declarations import ThreadStatus

        if self._comms.registry.status(thread_name) is ThreadStatus.STOPPED:
            return
        for message in self._comms.inbox(thread_name):
            if self._client is None:
                break
            await self._client.session_update(
                session_id=session_id,
                update=AgentMessageChunk(
                    session_update="agent_message_chunk",
                    content=TextContentBlock(
                        type="text", text=f"[{message.sender}] {message.body}"
                    ),
                ),
            )
        self._comms.acknowledge(thread_name)

    async def _run_agent_turn(self, session_id: str, thread_name: str, task: str) -> None:
        """Stream a real coding agent's reply into the session and the wire."""
        thread = self._comms.registry.require(thread_name)
        if shutil.which(self._agent_bin) is None:
            await self._emit_text(
                session_id,
                f"agent backend {self._agent_bin!r} not found on PATH; "
                "reply relayed to the room only",
            )
            return
        worktree = thread.worktree if Path(thread.worktree).is_dir() else str(Path.cwd())
        env = os.environ.copy()
        env.setdefault("AGENT_COMMS_THREAD", thread_name)
        try:
            proc = await asyncio.create_subprocess_exec(
                self._agent_bin,
                *self._agent_args,
                task,
                cwd=worktree,
                env=env,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
                stdin=asyncio.subprocess.DEVNULL,
            )
        except OSError as exc:
            await self._emit_text(session_id, f"agent launch failed: {exc}")
            return
        assert proc.stdout is not None
        reply: list[str] = []
        while True:
            chunk = await proc.stdout.read(4096)
            if not chunk:
                break
            piece = chunk.decode(errors="replace")
            reply.append(piece)
            await self._emit_text(session_id, piece)
        await proc.wait()
        body = "".join(reply).strip()
        if body:
            self._comms.send(thread_name, GLOBAL_TARGET, body[:4000])

    async def _emit_text(self, session_id: str, text: str) -> None:
        if self._client is None or not text:
            return
        await self._client.session_update(
            session_id=session_id,
            update=AgentMessageChunk(
                session_update="agent_message_chunk",
                content=TextContentBlock(type="text", text=text),
            ),
        )

    @staticmethod
    def _prompt_text(prompt: list[Any]) -> str:
        chunks: list[str] = []
        for block in prompt:
            if isinstance(block, dict):
                chunks.append(str(block.get("text", "")))
            else:
                chunks.append(str(getattr(block, "text", "") or ""))
        return "\n".join(chunk for chunk in chunks if chunk)


def main() -> int:
    comms = wire()

    async def run() -> None:
        await run_agent(CommsAgent(comms))  # type: ignore[arg-type]

    asyncio.run(run())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
