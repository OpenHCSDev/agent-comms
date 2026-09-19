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
import json
import os
import re
import time
from pathlib import Path
from typing import Any, cast

from acp import RequestError, run_agent
from acp.schema import (
    AgentCapabilities,
    AgentMessageChunk,
    AgentThoughtChunk,
    ContentToolCallContent,
    Implementation,
    InitializeResponse,
    NewSessionResponse,
    PromptResponse,
    TextContentBlock,
    ToolCallProgress,
    ToolCallStart,
)

from . import backend
from .declarations import ActivityState
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
NO_REPLY_WINDOW = 2.5  # silence: end the turn after this long with nothing
REPLY_WINDOW = 8.0  # once replies flow, keep collecting at most this long
REPLY_QUIET = 1.5  # after the last reply, wait this long then end the turn
REPLY_POLL = 0.25
ACTIVITY_WINDOW = 60.0  # keep the turn open while a peer is thinking/working
IDLE_GRACE = 1.0  # peers idle for this long -> drain and end the turn


class CommsAgent:
    """ACP agent bound to one Comms wire.

    Session -> thread. A prompt broadcasts the text to the session's thread
    on the global channel, drains the thread's inbox back to the client as
    agent messages, then acknowledges.
    """

    def __init__(
        self,
        comms: Comms,
        agent_bin: str | None = None,
        agent_args: list[str] | None = None,
        reply_window: float | None = None,
        no_reply_window: float | None = None,
        reply_quiet: float | None = None,
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
        self._reply_window = (
            reply_window
            if reply_window is not None
            else float(os.environ.get("AGENT_COMMS_REPLY_WINDOW", str(REPLY_WINDOW)))
        )
        self._no_reply_window = (
            no_reply_window
            if no_reply_window is not None
            else float(os.environ.get("AGENT_COMMS_NO_REPLY_WINDOW", str(NO_REPLY_WINDOW)))
        )
        self._reply_quiet = (
            reply_quiet
            if reply_quiet is not None
            else float(os.environ.get("AGENT_COMMS_REPLY_QUIET", str(REPLY_QUIET)))
        )

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
        target, body = parse_target(text)
        sent_seq = 0
        if body:
            self._comms.send(thread_name, target, body)
            sent_seq = self._comms.bus.total_messages()
        self._debug_log(f"prompt:start sender={thread_name} target={target} sent_seq={sent_seq}")
        await self._drain_inbox(session_id, thread_name)
        if agent_task:
            await self._run_agent_turn(session_id, thread_name, agent_task)
        # Hold the turn open while replies arrive: clients render updates
        # during a turn, so the room's answers stream into the chat live.
        await self._collect_replies(session_id, thread_name, sent_seq)
        self._debug_log("prompt:returning")
        self._ensure_live_drain(session_id, thread_name)
        return PromptResponse(stop_reason="end_turn")

    def _debug_log(self, message: str) -> None:
        debug_path = os.environ.get("AGENT_COMMS_DEBUG_LOG")
        if debug_path:
            with open(debug_path, "a") as debug_log:
                debug_log.write(f"[{time.time():.3f}] {message}\n")

    async def _collect_replies(self, session_id: str, thread_name: str, sent_seq: int = 0) -> None:
        """Stream inbox messages into the open turn until quiet or timeout.

        While a peer is on it (activity thinking/working, or a peer's read
        marker past our message), the turn stays open — the client shows
        its spinner, and replies drain the moment they land.
        """
        waited = 0.0
        quiet = 0.0
        got_reply = False
        idle_for = 0.0
        debug_path = os.environ.get("AGENT_COMMS_DEBUG_LOG")
        while True:
            await asyncio.sleep(REPLY_POLL)
            waited += REPLY_POLL
            quiet += REPLY_POLL
            pushed = await self._drain_count(session_id, thread_name)
            if pushed:
                got_reply = True
                quiet = 0.0
                idle_for = 0.0
            if got_reply:
                if quiet >= self._reply_quiet or waited >= self._reply_window:
                    self._debug_log(f"collect:break got_reply waited={waited}")
                    break
                continue
            # No reply yet: keep waiting while a peer is on it.
            peer_progress = self._peer_progress(thread_name, sent_seq)
            idle_for = 0.0 if peer_progress else idle_for + REPLY_POLL
            if idle_for >= IDLE_GRACE and waited >= self._no_reply_window:
                break
            if waited >= ACTIVITY_WINDOW:
                break

    def _peer_progress(self, thread_name: str, sent_seq: int) -> bool:
        """True when a peer is active on, or has read, our message."""
        for name, activity in self._comms.all_activity().items():
            if name != thread_name and activity.state is not ActivityState.IDLE:
                return True
        if sent_seq:
            markers = self._comms.bus._read_markers()
            for name, marker in markers.items():
                if name != thread_name and marker >= sent_seq:
                    return True
        return False

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
                try:
                    await self._drain_inbox(session_id, thread_name)
                except asyncio.CancelledError:
                    raise
                except Exception as error:
                    # Never let the drain task die silently: a dead drain
                    # means replies stop reaching the client.
                    self._debug_log(f"live-drain error: {error!r}")

        task = asyncio.create_task(loop())
        self._drain_tasks[session_id] = task

    async def _drain_inbox(self, session_id: str, thread_name: str) -> int:
        """Push undelivered messages to the client; returns count pushed."""
        from .declarations import ThreadStatus

        if self._comms.registry.status(thread_name) is ThreadStatus.STOPPED:
            return 0
        pushed = 0
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
            pushed += 1
        self._comms.acknowledge(thread_name)
        return pushed

    async def _drain_count(self, session_id: str, thread_name: str) -> int:
        return await self._drain_inbox(session_id, thread_name)

    async def _run_agent_turn(self, session_id: str, thread_name: str, task: str) -> None:
        """Stream a real coding agent's reply: events to the client, status to the wire."""
        thread = self._comms.registry.require(thread_name)
        self._comms.set_activity(thread_name, ActivityState.THINKING, task[:80])
        await self._emit_event(session_id, {"type": "thinking", "text": f"thinking: {task[:80]}"})
        worktree = thread.worktree if Path(thread.worktree).is_dir() else str(Path.cwd())
        env_extra = {"AGENT_COMMS_THREAD": thread_name}
        reply_parts: list[str] = []
        try:
            async for event in backend.stream_agent_events(
                self._agent_bin, self._agent_args, task, worktree, env_extra
            ):
                kind = event.get("type")
                if kind == "chunk":
                    reply_parts.append(event.get("text") or "")
                elif kind == "tool_start":
                    self._comms.set_activity(
                        thread_name, ActivityState.WORKING, event.get("title", "")
                    )
                elif kind == "tool_end" and event.get("ok"):
                    self._comms.set_activity(thread_name, ActivityState.THINKING, task[:80])
                await self._emit_event(session_id, event)
        finally:
            body = "".join(reply_parts).strip()
            if body:
                self._comms.send(thread_name, GLOBAL_TARGET, body[:4000])
            self._comms.set_activity(thread_name, ActivityState.IDLE)

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

    # ─── Unified event forwarding ─────────────────────────────────────────────
    # One abstraction maps server events to ACP session updates. Toad, Zed,
    # and VS Code render these natively; the same events also feed the
    # wire-level activity log for headless clients.

    async def _emit_event(self, session_id: str, event: dict[str, Any]) -> None:
        """Forward one backend/wire event to the ACP client."""
        if self._client is None:
            return
        kind = event.get("type")
        if kind == "chunk":
            text = event.get("text") or ""
            if text:
                await self._emit_text(session_id, text)
        elif kind == "tool_start":
            await self._client.session_update(
                session_id=session_id,
                update=ToolCallStart(
                    session_update="tool_call",
                    tool_call_id=event["id"],
                    title=event.get("title") or event.get("name") or "tool",
                    kind=cast(Any, backend.tool_kind(event.get("name") or "other")),
                    status="in_progress",
                ),
            )
        elif kind == "tool_end":
            update = ToolCallProgress(
                session_update="tool_call_update",
                tool_call_id=event["id"],
                status="completed" if event.get("ok") else "failed",
            )
            output = event.get("output") or ""
            if output:
                update.content = [
                    ContentToolCallContent(
                        type="content",
                        content=TextContentBlock(type="text", text=output),
                    )
                ]
            await self._client.session_update(session_id=session_id, update=update)
        elif kind == "thinking":
            await self._client.session_update(
                session_id=session_id,
                update=AgentThoughtChunk(
                    session_update="agent_thought_chunk",
                    content=TextContentBlock(type="text", text=event.get("text") or ""),
                ),
            )
        elif kind == "done":
            if not event.get("ok") and event.get("text"):
                await self._emit_text(session_id, f"[agent error] {event['text']}")

    @staticmethod
    def _prompt_text(prompt: list[Any]) -> str:
        chunks: list[str] = []
        for block in prompt:
            if isinstance(block, dict):
                chunks.append(str(block.get("text", "")))
            else:
                chunks.append(str(getattr(block, "text", "") or ""))
        return "\n".join(chunk for chunk in chunks if chunk)


def parse_target(text: str) -> tuple[str, str]:
    """Split a leading target prefix off a prompt.

    ``@name body`` -> DM; ``#channel body`` -> channel; empty body with a
    bare target composes nothing; otherwise the global channel. The prefix
    is consumed, never broadcast.
    """
    stripped = text.strip()
    if stripped.startswith("@") and len(stripped) > 1:
        parts = stripped[1:].split(maxsplit=1)
        name = parts[0].rstrip("@#")
        if name and len(parts) == 2 and parts[1].strip():
            return name, parts[1].strip()
    if stripped.startswith("#") and len(stripped) > 1:
        parts = stripped[1:].split(maxsplit=1)
        channel = parts[0].rstrip("@#")
        if channel and len(parts) == 2 and parts[1].strip():
            return f"#{channel}", parts[1].strip()
    return GLOBAL_TARGET, stripped


def main() -> int:
    debug_path = os.environ.get("AGENT_COMMS_DEBUG_LOG")
    if debug_path:
        import logging

        logging.basicConfig(
            level=logging.DEBUG,
            format="%(asctime)s %(name)s %(levelname)s %(message)s",
            filename=debug_path + ".log",
        )
    comms = wire()

    async def run() -> None:
        if os.environ.get("AGENT_COMMS_DEBUG_LOG"):

            async def watchdog() -> None:
                while True:
                    await asyncio.sleep(5)
                    tasks = [t for t in asyncio.all_tasks() if not t.done()]
                    with open(os.environ["AGENT_COMMS_DEBUG_LOG"], "a") as debug_log:
                        debug_log.write(f"=== watchdog: {len(tasks)} tasks ===\n")
                        for task in tasks:
                            stack = task.get_stack()
                            innermost = [
                                f"{frame.f_code.co_filename.split('/')[-1]}:{frame.f_lineno}"
                                for frame in stack
                                if frame
                            ][-4:]
                            debug_log.write(f"  {task.get_name()}: {' <- '.join(innermost)}\n")

            asyncio.create_task(watchdog())
        agent = CommsAgent(comms)

        def observe(event: Any) -> None:
            agent._debug_log(
                f"{event.direction.value}: {json.dumps(event.message)[:200]}"
                if hasattr(event, "message")
                else f"{event.direction.value}"
            )

        conn_kwargs: dict[str, Any] = {}
        if os.environ.get("AGENT_COMMS_DEBUG_LOG"):
            conn_kwargs["observers"] = [observe]
        await run_agent(agent, **conn_kwargs)  # type: ignore[arg-type]

    asyncio.run(run())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
