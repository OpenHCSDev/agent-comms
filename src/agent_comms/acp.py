"""Agent communications — ACP server on the official protocol library.

Implements an Agent Client Protocol agent so ACP clients (Toad, Zed, VS Code)
attach to the coordination wire natively: each client session is one thread
on the wire, prompts are messages on the bus, and replies come back as
``agent_message_chunk`` session updates.

Two turn modes share one session:

- **agent** (default): the prompt runs a real coding agent (pi, headless
  ``--print`` mode) and streams its thinking, tools, and response.
- **relay**: ``@peer``, ``#channel``, or ``!relay`` sends directly to the
  coordination wire without launching another coding turn.

The explicit ``!agent`` prefix remains accepted for compatibility.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import time
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

from acp import RequestError, run_agent
from acp.schema import (
    AgentCapabilities,
    AgentMessageChunk,
    AgentThoughtChunk,
    ContentToolCallContent,
    Implementation,
    InitializeResponse,
    LoadSessionResponse,
    NewSessionResponse,
    PromptResponse,
    SessionInfoUpdate,
    TextContentBlock,
    ToolCallProgress,
    ToolCallStart,
    UsageUpdate,
    UserMessageChunk,
)

from . import backend
from .declarations import ActivityState, Thread, ThreadStatus
from .operations import Comms, wire
from .runtime import RuntimeProxy, RuntimeServer, socket_path
from .tool_results import tool_result_content

GLOBAL_TARGET = "#all"
AGENT_PREFIX = "!agent "
RELAY_PREFIX = "!relay "
DEFAULT_AGENT_BIN = "pi"
DEFAULT_AGENT_ARGS = [
    "--print",
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

    Session -> thread. Coding prompts run the configured backend; targeted
    prompts relay through the shared wire and drain replies back to the client.
    """

    def __init__(
        self,
        comms: Comms,
        agent_bin: str | None = None,
        agent_args: list[str] | None = None,
        reply_window: float | None = None,
        no_reply_window: float | None = None,
        reply_quiet: float | None = None,
        runtime_enabled: bool = False,
        auto_wake: bool = True,
    ):
        self._comms = comms
        self._sessions: dict[str, str] = {}
        self._client: Any = None
        self._agent_bin = agent_bin or os.environ.get("AGENT_COMMS_AGENT_BIN", DEFAULT_AGENT_BIN)
        arg_env = os.environ.get("AGENT_COMMS_AGENT_ARGS", "")
        self._agent_args = (
            agent_args
            if agent_args is not None
            else (arg_env.split() if arg_env else list(DEFAULT_AGENT_ARGS))
        )
        self._drain_tasks: dict[str, asyncio.Task[None]] = {}
        self._turn_tasks: dict[str, asyncio.Task[Any]] = {}
        self._backend_inboxes: dict[str, asyncio.Queue[str]] = {}
        self._session_titles: dict[str, str] = {}
        self._runtime_enabled = runtime_enabled
        self._runtime = RuntimeServer(self)
        self._proxies: dict[str, RuntimeProxy] = {}
        self._auto_wake = auto_wake
        self._pending_turns: dict[str, list[str]] = {}
        self._drain_locks: dict[str, asyncio.Lock] = {}
        self._turn_locks: dict[str, asyncio.Lock] = {}
        self._closing = False
        self._inbox_cursors: dict[str, int] = {}
        self._wake_tasks: dict[str, asyncio.Task[None]] = {}
        self._active_turns: dict[str, str] = {}
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
            agent_capabilities=AgentCapabilities(load_session=True),
            agent_info=Implementation(name="agent-comms", title="Agent Comms", version="0.1.0"),
            auth_methods=[],
        )

    async def new_session(
        self, cwd: str, mcp_servers: list[Any] | None = None, **kwargs: Any
    ) -> NewSessionResponse:
        thread = self._comms.claim_thread(
            self._thread_name_for(cwd),
            tags=frozenset({"acp"}),
            worktree=cwd,
            pid=os.getpid(),
            start_at_latest=True,
        )
        thread_name = thread.name
        session_id = thread_name
        self._sessions[session_id] = thread_name
        self._inbox_cursors[session_id] = self._comms.message_high_water()
        self._session_titles[session_id] = thread_name
        if self._runtime_enabled:
            await self._runtime.start()
        self._ensure_live_drain(session_id)
        return NewSessionResponse(
            session_id=session_id,
            field_meta=self._session_metadata(thread_name),
        )

    async def load_session(
        self,
        cwd: str,
        session_id: str,
        mcp_servers: list[Any] | None = None,
        **kwargs: Any,
    ) -> LoadSessionResponse:
        """Reconnect an ACP client to its persistent wire thread."""
        thread = self._comms.registry.require(session_id)
        if Path(thread.worktree).resolve() != Path(cwd).resolve():
            raise RequestError.invalid_params(
                {"reason": "The saved thread belongs to a different working directory."}
            )
        thread = self._comms.acquire_thread(thread.name, owner_pid=os.getpid())
        if thread.pid != os.getpid():
            proxy = RuntimeProxy(self, session_id, socket_path(self._comms.root, thread.pid))
            try:
                metadata = await proxy.subscribe()
            except (OSError, RuntimeError) as error:
                await proxy.close()
                raise RequestError.invalid_params(
                    {
                        "reason": (
                            f"Thread {thread.name!r} is already owned by "
                            f"process {thread.pid}: {error}"
                        )
                    }
                ) from error
            self._proxies[session_id] = proxy
            return LoadSessionResponse(field_meta=metadata)
        self._comms.heartbeat(thread.name)
        self._sessions[session_id] = thread.name
        pending = self._comms.inbox(thread.name)
        self._inbox_cursors[session_id] = (
            pending[0].seq - 1 if pending else self._comms.message_high_water()
        )
        self._session_titles[session_id] = thread.name
        if self._runtime_enabled:
            await self._runtime.start()
        await self._replay_transcript(session_id, thread.name)
        self._ensure_live_drain(session_id)
        return LoadSessionResponse(field_meta=self._session_metadata(thread.name))

    async def _replay_transcript(self, session_id: str, name: str, client: Any = None) -> None:
        if getattr(client, "transcript_snapshots", False):
            page = await asyncio.to_thread(self._comms.thread_transcript_page, name)
            await client.session_update(
                session_id=session_id,
                update=AgentMessageChunk(
                    session_update="agent_message_chunk",
                    content=TextContentBlock(type="text", text=""),
                    field_meta={
                        "agentComms": {
                            "transcript": [
                                event.to_wire(
                                    include_diff=getattr(client, "transcript_diffs", False)
                                )
                                for event in page.events
                            ],
                            "transcriptPage": page.metadata(),
                        }
                    },
                ),
            )
            return
        for event in self._comms.thread_transcript(name):
            await self._emit_event(
                session_id,
                {
                    "type": event.kind,
                    "text": event.text,
                    "id": event.tool_call_id,
                    "name": event.tool_name,
                    "title": event.tool_name,
                    "args": event.raw_input,
                    "output": event.text,
                    "ok": event.ok,
                    "diff": event.diff,
                },
                client=client,
            )

    async def prompt(self, session_id: str, prompt: list[Any], **kwargs: Any) -> PromptResponse:
        if session_id in self._proxies:
            result = await self._proxies[session_id].request(
                "prompt",
                prompt=[
                    (
                        block
                        if isinstance(block, dict)
                        else block.model_dump(by_alias=True, exclude_none=True)
                    )
                    for block in prompt
                ],
            )
            return PromptResponse.model_validate(result)
        async with self._turn_locks.setdefault(session_id, asyncio.Lock()):
            return await self._prompt_owned(session_id, prompt)

    async def _prompt_owned(self, session_id: str, prompt: list[Any]) -> PromptResponse:
        turn_task = asyncio.current_task()
        assert turn_task is not None
        self._turn_tasks[session_id] = turn_task
        try:
            thread_name = await self._sync_session_identity(session_id)
            self._comms.registry.require(thread_name)
            text = self._prompt_text(prompt)
            agent_task: str | None = None
            relay_text: str | None = None
            if text.startswith(AGENT_PREFIX):
                agent_task = text[len(AGENT_PREFIX) :].strip()
            elif text.startswith(RELAY_PREFIX):
                relay_text = text[len(RELAY_PREFIX) :].strip()
            elif text.lstrip().startswith(("@", "#")):
                relay_text = text
            else:
                agent_task = text.strip()
            sent_seq = 0
            if relay_text:
                target, body = parse_target(relay_text)
                self._comms.send(thread_name, target, body)
                sent_seq = self._comms.bus.total_messages()
            self._debug_log(
                f"prompt:start sender={thread_name} mode={'agent' if agent_task else 'relay'} "
                f"sent_seq={sent_seq}"
            )
            if agent_task:
                await self._run_agent_turn(session_id, thread_name, agent_task)
            else:
                turn_id = uuid4().hex
                self._active_turns[session_id] = turn_id
                try:
                    await self._emit_event(session_id, {"type": "started", "turn_id": turn_id})
                    await self._drain_inbox(session_id)
                    await self._collect_replies(session_id, thread_name, sent_seq)
                finally:
                    self._active_turns.pop(session_id, None)
                    await self._emit_event(session_id, {"type": "settled", "turn_id": turn_id})
            self._debug_log("prompt:returning")
            return PromptResponse(stop_reason="end_turn")
        except asyncio.CancelledError:
            self._debug_log("prompt:cancelled")
            await backend.terminate_task_process(turn_task)
            return PromptResponse(stop_reason="cancelled")
        finally:
            if self._turn_tasks.get(session_id) is turn_task:
                self._turn_tasks.pop(session_id, None)
            self._ensure_live_drain(session_id)

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
        while True:
            await asyncio.sleep(REPLY_POLL)
            waited += REPLY_POLL
            quiet += REPLY_POLL
            pushed = await self._drain_count(session_id)
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
        if session_id in self._proxies:
            await self._proxies[session_id].request("cancel")
            return
        task = self._turn_tasks.get(session_id)
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            await backend.terminate_task_process(task)
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

    def _session_metadata(self, thread_name: str) -> dict[str, Any]:
        return {
            "agentComms": {
                "thread": thread_name,
                "wireRoot": str(self._comms.root.resolve()),
                "persistence": "shared on-disk wire",
                "transport": "per-session stdio ACP",
                "ownerPid": os.getpid(),
                "turnLifecycle": True,
            }
        }

    async def _sync_session_identity(self, session_id: str) -> str:
        """Follow permanent aliases and publish the canonical wire name."""
        cached_name = self._require_session(session_id)
        thread_name = self._comms.registry.require(cached_name).name
        self._sessions[session_id] = thread_name
        if self._session_titles.get(session_id) != thread_name:
            await self._runtime.session_update(
                session_id=session_id,
                update=SessionInfoUpdate(
                    session_update="session_info_update",
                    title=thread_name,
                ),
            )
            self._session_titles[session_id] = thread_name
        return thread_name

    def _ensure_live_drain(self, session_id: str) -> None:
        """Keep one background task per session pushing inbox messages live."""
        if self._closing:
            return
        if session_id in self._drain_tasks and not self._drain_tasks[session_id].done():
            return

        async def loop() -> None:
            while True:
                await asyncio.sleep(LIVE_DRAIN_INTERVAL)
                try:
                    await self._drain_inbox(session_id)
                except asyncio.CancelledError:
                    raise
                except Exception as error:
                    # Never let the drain task die silently: a dead drain
                    # means replies stop reaching the client.
                    self._debug_log(f"live-drain error: {error!r}")

        task = asyncio.create_task(loop())
        self._drain_tasks[session_id] = task

    async def _drain_inbox(self, session_id: str) -> int:
        """Push undelivered messages to the client; returns count pushed."""
        async with self._drain_locks.setdefault(session_id, asyncio.Lock()):
            return await self._drain_owned_inbox(session_id)

    async def _drain_owned_inbox(self, session_id: str) -> int:
        thread_name = await self._sync_session_identity(session_id)
        if self._comms.registry.status(thread_name) is ThreadStatus.STOPPED:
            return 0
        backend_inbox = self._backend_inboxes.get(session_id)
        if self._client is None and backend_inbox is None and not self._runtime_enabled:
            return 0
        pushed = 0
        after = self._inbox_cursors.get(session_id, 0)
        for message in self._comms.incoming_page(thread_name, after=after).messages:
            incoming_task = (
                f"[agent-comms from {message.sender} to {message.target}]\n{message.body}"
            )
            direct = self._comms.registry.canonical_name(message.target) == thread_name
            if backend_inbox is not None and direct:
                backend_inbox.put_nowait(incoming_task)
            elif self._auto_wake and self._runtime_enabled and direct:
                self._pending_turns.setdefault(session_id, []).append(incoming_task)
            await self._runtime.session_update(
                session_id=session_id,
                update=AgentMessageChunk(
                    session_update="agent_message_chunk",
                    content=TextContentBlock(
                        type="text", text=f"Incoming from {message.sender}:\n{message.body}\n"
                    ),
                    field_meta={
                        "agentComms": {
                            "incoming": {
                                "sender": message.sender,
                                "target": message.target,
                                "body": message.body,
                                "sequence": message.seq,
                            }
                        }
                    },
                ),
            )
            self._inbox_cursors[session_id] = message.seq
            pushed += 1
        if pushed:
            self._comms.acknowledge_through(thread_name, self._inbox_cursors[session_id])
        self._schedule_wake(session_id)
        return pushed

    def _schedule_wake(self, session_id: str) -> None:
        if (
            self._closing
            or not self._auto_wake
            or not self._runtime_enabled
            or not self._pending_turns.get(session_id)
        ):
            return
        if session_id in self._wake_tasks and not self._wake_tasks[session_id].done():
            return

        async def wake() -> None:
            while self._pending_turns.get(session_id) and not self._closing:
                async with self._turn_locks.setdefault(session_id, asyncio.Lock()):
                    pending = self._pending_turns.pop(session_id, [])
                    self._turn_tasks[session_id] = asyncio.current_task()  # type: ignore[assignment]
                    try:
                        await self._run_agent_turn(
                            session_id, self._require_session(session_id), "\n\n".join(pending)
                        )
                    finally:
                        self._turn_tasks.pop(session_id, None)

        self._wake_tasks[session_id] = asyncio.create_task(wake())

    async def _drain_count(self, session_id: str) -> int:
        return await self._drain_inbox(session_id)

    async def _run_agent_turn(self, session_id: str, thread_name: str, task: str) -> None:
        """Stream a real coding agent's reply: events to the client, status to the wire."""
        thread = self._comms.registry.require(thread_name)
        thread_name = thread.name
        self._sessions[session_id] = thread_name
        self._comms.set_activity(thread_name, ActivityState.THINKING, task[:80])
        worktree = thread.worktree if Path(thread.worktree).is_dir() else str(Path.cwd())
        env_extra = {
            "AGENT_COMMS_THREAD": thread_name,
            "PI_AGENT_ID": thread_name,
            "AGENT_COMMS_ROOT": str(self._comms.root),
            "PI_PARENT_ID": thread.parent or "",
            "AGENT_COMMS_MANAGED": "1",
        }
        peers = [
            {key: person[key] for key in ("name", "status", "activity", "activity_detail")}
            for person in self._comms.presence()
            if person["name"] != thread_name
        ][:50]
        task = (
            f"Coordination context: you are thread {thread_name!r}; parent={thread.parent!r}. "
            "This identity overrides identities in inherited conversation history. "
            "Use your own identity for comms tools. Incoming direct messages automatically "
            "start a new turn when you are idle, or are delivered into your current turn. "
            "End your turn when done; never sleep or poll waiting for messages. "
            "Reply with comms_send when a reply is useful; do not echo acknowledgments. "
            f"Peer state: {json.dumps(peers)}\n\n{task}"
        )
        reply_parts: list[str] = []
        settled = False
        backend_inbox: asyncio.Queue[str] = asyncio.Queue()
        finish_event = asyncio.Event()
        self._backend_inboxes[session_id] = backend_inbox
        turn_id = uuid4().hex
        self._active_turns[session_id] = turn_id
        try:
            await self._emit_event(session_id, {"type": "started", "turn_id": turn_id})
            await self._drain_inbox(session_id)
            session_file = thread.session_file
            fork_session = False
            if not session_file and thread.parent:
                session_file = self._comms.registry.require(thread.parent).session_file
                fork_session = bool(session_file)
            async for event in backend.stream_agent_events(
                self._agent_bin,
                self._agent_args,
                task,
                worktree,
                env_extra,
                session_file=session_file,
                fork_session=fork_session,
                steering_queue=backend_inbox,
                finish_event=finish_event,
            ):
                kind = event.get("type")
                if kind == "chunk":
                    reply_parts.append(event.get("text") or "")
                elif kind == "agent_info":
                    session_name = event.get("session_name")
                    session_file = event.get("session_file")
                    if session_file:
                        current = self._comms.registry.require(thread_name)
                        self._comms.register(
                            Thread(
                                name=current.name,
                                tags=current.tags,
                                worktree=current.worktree,
                                parent=current.parent,
                                task=current.task,
                                pid=current.pid,
                                session_file=str(session_file),
                            )
                        )
                    self._comms.set_agent_info(
                        thread_name,
                        model=event.get("model"),
                        session_name=session_name,
                        context_used=event.get("context_used"),
                        context_size=event.get("context_size"),
                    )
                elif kind == "tool_start":
                    self._comms.set_activity(
                        thread_name, ActivityState.WORKING, event.get("title", "")
                    )
                elif kind == "tool_end":
                    thread_name = await self._sync_session_identity(session_id)
                    self._comms.set_activity(thread_name, ActivityState.THINKING, task[:80])
                elif kind == "settled":
                    self._comms.set_activity(thread_name, ActivityState.IDLE)
                    settled = True
                    finish_event.set()
                    self._active_turns.pop(session_id, None)
                await self._emit_event(session_id, {**event, "turn_id": turn_id})
        finally:
            if self._backend_inboxes.get(session_id) is backend_inbox:
                self._backend_inboxes.pop(session_id, None)
            while not backend_inbox.empty():
                self._pending_turns.setdefault(session_id, []).append(backend_inbox.get_nowait())
            thread_name = await self._sync_session_identity(session_id)
            body = "".join(reply_parts).strip()
            if body:
                self._comms.send(thread_name, GLOBAL_TARGET, body[:4000])
            if not settled:
                self._comms.set_activity(thread_name, ActivityState.IDLE)
                self._active_turns.pop(session_id, None)
                await self._emit_event(session_id, {"type": "settled", "turn_id": turn_id})

    async def replay_turn_state(self, session_id: str, client: Any = None) -> None:
        turn_id = self._active_turns.get(session_id)
        await self._emit_event(
            session_id,
            {"type": "started" if turn_id else "settled", "turn_id": turn_id or ""},
            client=client,
        )

    async def shutdown(self) -> None:
        """Stop drains and mark threads owned by this ACP connection offline."""
        self._closing = True
        for wake_task in self._wake_tasks.values():
            wake_task.cancel()
        await asyncio.gather(*self._wake_tasks.values(), return_exceptions=True)
        for proxy in self._proxies.values():
            await proxy.close()
        turns = list(self._turn_tasks.values())
        self._turn_tasks.clear()
        for task in turns:
            task.cancel()
        if turns:
            await asyncio.gather(*turns, return_exceptions=True)
            await asyncio.gather(
                *(backend.terminate_task_process(task) for task in turns),
                return_exceptions=True,
            )
        tasks = list(self._drain_tasks.values())
        self._drain_tasks.clear()
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        for name in set(self._sessions.values()):
            try:
                canonical = self._comms.registry.require(name).name
                if (
                    self._comms.registry.require(canonical).pid == os.getpid()
                    and self._comms.registry.status(canonical) is ThreadStatus.RUNNING
                ):
                    self._comms.stop(canonical)
            except Exception as error:
                self._debug_log(f"shutdown error: {error!r}")
        await self._runtime.close()

    async def _emit_text(self, session_id: str, text: str, client: Any = None) -> None:
        if not text:
            return
        await (client or self._runtime).session_update(
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

    async def _emit_event(self, session_id: str, event: dict[str, Any], client: Any = None) -> None:
        """Forward one backend/wire event to the ACP client."""
        client = client or self._runtime
        kind = event.get("type")
        if kind == "user":
            text = event.get("text") or ""
            if text:
                await client.session_update(
                    session_id=session_id,
                    update=UserMessageChunk(
                        session_update="user_message_chunk",
                        content=TextContentBlock(type="text", text=text),
                    ),
                )
        elif kind in {"chunk", "assistant", "notice"}:
            text = event.get("text") or ""
            if text:
                await self._emit_text(session_id, text, client)
        elif kind == "tool_start":
            start_update = ToolCallStart(
                session_update="tool_call",
                tool_call_id=event["id"],
                title=event.get("title") or event.get("name") or "tool",
                kind=cast(Any, backend.tool_kind(event.get("name") or "other")),
                status="in_progress",
            )
            if event.get("args") is not None:
                start_update.raw_input = event["args"]
            await client.session_update(
                session_id=session_id,
                update=start_update,
            )
        elif kind == "tool_progress":
            progress_update = ToolCallProgress(
                session_update="tool_call_update",
                tool_call_id=event["id"],
                status="in_progress",
            )
            output = event.get("output") or ""
            if output:
                progress_update.content = [
                    ContentToolCallContent(
                        type="content",
                        content=TextContentBlock(type="text", text=output),
                    )
                ]
            await client.session_update(session_id=session_id, update=progress_update)
        elif kind == "tool_end":
            end_update = ToolCallProgress(
                session_update="tool_call_update",
                tool_call_id=event["id"],
                status="completed" if event.get("ok") else "failed",
            )
            end_update.content = [
                ContentToolCallContent.model_validate(item)
                for item in tool_result_content(
                    event["id"], event.get("output") or "", event.get("diff")
                )
            ]
            await client.session_update(session_id=session_id, update=end_update)
        elif kind == "thinking":
            await client.session_update(
                session_id=session_id,
                update=AgentThoughtChunk(
                    session_update="agent_thought_chunk",
                    content=TextContentBlock(type="text", text=event.get("text") or ""),
                ),
            )
        elif kind == "agent_info":
            used = event.get("context_used")
            size = event.get("context_size")
            if used is not None and size:
                await client.session_update(
                    session_id=session_id,
                    update=UsageUpdate(
                        session_update="usage_update",
                        used=used,
                        size=size,
                    ),
                )
        elif kind in {"started", "settled"}:
            await client.session_update(
                session_id=session_id,
                update=AgentMessageChunk(
                    session_update="agent_message_chunk",
                    content=TextContentBlock(type="text", text=""),
                    field_meta={
                        "agentComms": {
                            "turnStarted" if kind == "started" else "turnSettled": True,
                            "turnId": event.get("turn_id"),
                        }
                    },
                ),
            )
        elif kind == "done":
            if not event.get("ok") and event.get("text"):
                await self._emit_text(session_id, f"[agent error] {event['text']}", client)

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
        agent = CommsAgent(comms, runtime_enabled=True)

        def observe(event: Any) -> None:
            agent._debug_log(
                f"{event.direction.value}: {json.dumps(event.message)[:200]}"
                if hasattr(event, "message")
                else f"{event.direction.value}"
            )

        conn_kwargs: dict[str, Any] = {}
        if os.environ.get("AGENT_COMMS_DEBUG_LOG"):
            conn_kwargs["observers"] = [observe]
        try:
            await run_agent(agent, **conn_kwargs)  # type: ignore[arg-type]
        finally:
            await agent.shutdown()

    asyncio.run(run())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
