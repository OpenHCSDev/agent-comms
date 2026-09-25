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
import shlex
import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

from acp import RequestError, run_agent
from acp.schema import (
    AgentCapabilities,
    AgentMessageChunk,
    AgentThoughtChunk,
    ConfigOptionUpdate,
    ContentToolCallContent,
    Implementation,
    InitializeResponse,
    LoadSessionResponse,
    NewSessionResponse,
    PermissionOption,
    PromptCapabilities,
    PromptResponse,
    RequestPermissionResponse,
    SessionConfigOptionSelect,
    SessionConfigSelectOption,
    SessionInfoUpdate,
    SetSessionConfigOptionResponse,
    TerminalAuthMethod,
    TextContentBlock,
    ToolCallProgress,
    ToolCallStart,
    ToolCallUpdate,
    UsageUpdate,
    UserMessageChunk,
)

from . import backend
from .declarations import (
    ActivityState,
    Goal,
    GoalExecution,
    Message,
    MessageRoute,
    MessageType,
    ScheduledTurn,
    Thread,
    TurnRouting,
    _store_lock,
    is_channel_target,
)
from .goal_attempts import (
    Generation,
    GoalAttemptError,
    GoalAttemptStore,
    LaunchPermit,
    StaleAttempt,
    UnresolvedAttempt,
)
from .input_disposition import AcpDeliveryCursors, InputDispositions
from .operations import OBSERVATION_INTERVAL, Comms, wire
from .runtime import (
    ACP_PERMISSION_TIMEOUT_SECONDS,
    UNBOUND_CONTROLLER,
    RuntimeProxy,
    RuntimeServer,
    SocketClient,
    socket_path,
)
from .tool_results import tool_result_content
from .wire_watch import open_wire_watcher

GLOBAL_TARGET = "#all"
AGENT_PREFIX = "!agent "
RELAY_PREFIX = "!relay "
GOAL_CONTINUE_PROMPT = "Continue working toward the active goal."
DEFAULT_AGENT_BIN = "pi"
DEFAULT_AGENT_ARGS = [
    "--print",
    "--provider",
    "openrouter",
    "--model",
    "z-ai/glm-5.3-flash",
]
LIVE_DRAIN_INTERVAL = OBSERVATION_INTERVAL
WATCH_FALLBACK_INTERVAL = 1.0
NO_REPLY_WINDOW = 2.5  # silence: end the turn after this long with nothing
REPLY_WINDOW = 8.0  # once replies flow, keep collecting at most this long
REPLY_QUIET = 1.5  # after the last reply, wait this long then end the turn
REPLY_POLL = 0.25
ACTIVITY_WINDOW = 60.0  # keep the turn open while a peer is thinking/working
IDLE_GRACE = 1.0  # peers idle for this long -> drain and end the turn


@dataclass(frozen=True, slots=True)
class QueuedInput:
    text: str
    echo: bool


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
        arg_env = os.environ.get("AGENT_COMMS_AGENT_ARGS")
        self._agent_args = (
            agent_args
            if agent_args is not None
            else (shlex.split(arg_env) if arg_env is not None else list(DEFAULT_AGENT_ARGS))
        )
        self._drain_tasks: dict[str, asyncio.Task[None]] = {}
        self._turn_tasks: dict[str, asyncio.Task[Any]] = {}
        self._backend_inboxes: dict[str, asyncio.Queue[str | dict[str, Any]]] = {}
        self._persistent_backends: dict[str, backend.PersistentPiSession] = {}
        self._queued_inputs: dict[str, dict[str, QueuedInput]] = {}
        # ACK/queue insertion is not model-read. Every accepted follow-up,
        # including non-displayed steers, needs its own identified user start.
        self._forwarded_inputs: dict[str, set[str]] = {}
        self._steering_input_keys: dict[str, dict[str, str]] = {}
        self._steering_origins: dict[str, dict[str, Message]] = {}
        self._steering_goal_ids: dict[str, dict[str, str | None]] = {}
        self._turn_input_keys: dict[str, set[str]] = {}
        self._dispositions = InputDispositions(comms.root)
        self._goal_store: GoalAttemptStore | None = None
        self._pending_goal_origins: dict[str, str] = {}
        self._delivery_cursors = AcpDeliveryCursors(comms.root)
        self._legacy_through: dict[str, int] = {}
        self._session_titles: dict[str, str] = {}
        self._display_titles: dict[str, str | None] = {}
        self._session_worktrees: dict[str, str] = {}
        self._runtime_enabled = runtime_enabled
        self._runtime = RuntimeServer(self)
        self._proxies: dict[str, RuntimeProxy] = {}
        self._proxy_image_support: dict[str, bool] = {}
        self._auto_wake = auto_wake
        self._pending_turns: dict[str, list[ScheduledTurn]] = {}
        self._drain_locks: dict[str, asyncio.Lock] = {}
        self._turn_locks: dict[str, asyncio.Lock] = {}
        self._closing = False
        self._inbox_cursors: dict[str, int] = {}
        self._wake_tasks: dict[str, asyncio.Task[None]] = {}
        self._active_turns: dict[str, str] = {}
        self._emitted_errors: dict[str, str] = {}
        self._model_catalog: list[backend.Model] | None = None
        self._model_catalog_auth: tuple[int, int] | None = None
        self._model_catalog_lock = asyncio.Lock()
        self._catalog_publish_lock = asyncio.Lock()
        self._catalog_generation = 0
        self._session_catalog_generation: dict[str, int] = {}
        self._session_config_signature: dict[str, tuple[str | None, str | None]] = {}
        self._goal_execution_signatures: dict[str, tuple[Goal | None, GoalExecution | None]] = {}
        self._transcript_snapshots = False
        self._transcript_diffs = False
        self._model_requests: dict[str, asyncio.Future[None]] = {}
        self._thinking_requests: dict[str, asyncio.Future[None]] = {}
        self._thinking_catalog: dict[tuple[str | None, tuple[int, int]], list[str]] = {}
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
        meta = (
            client_capabilities.get("_meta", {})
            if isinstance(client_capabilities, dict)
            else getattr(client_capabilities, "field_meta", None) or {}
        )
        self._transcript_snapshots = meta.get("agentComms", {}).get("transcriptSnapshots") is True
        self._transcript_diffs = meta.get("agentComms", {}).get("transcriptDiffs") is True
        capabilities = (
            client_capabilities
            if isinstance(client_capabilities, dict)
            else (
                client_capabilities.model_dump(by_alias=True, exclude_none=True)
                if client_capabilities is not None
                else {}
            )
        )
        methods: list[Any] = []
        if (capabilities.get("auth") or {}).get("terminal") and backend.rpc_args_for(
            self._agent_bin, []
        ) is not None:
            methods = [
                TerminalAuthMethod(
                    type="terminal",
                    id=f"pi-login-{provider or 'all'}",
                    name=name,
                    description="Sign in using Pi's native provider UI",
                    args=["--login", provider] if provider else ["--login"],
                )
                for provider, name in (
                    ("openai-codex", "ChatGPT subscription"),
                    ("openrouter", "OpenRouter"),
                    ("openai", "OpenAI API"),
                    ("", "Other Pi provider"),
                )
            ]
        return InitializeResponse(
            protocol_version=protocol_version,
            agent_capabilities=AgentCapabilities(
                load_session=True,
                prompt_capabilities=PromptCapabilities(
                    image=backend.rpc_args_for(self._agent_bin, self._agent_args) is not None
                ),
            ),
            agent_info=Implementation(name="agent-comms", title="Agent Comms", version="0.1.0"),
            auth_methods=methods,
        )

    def _declare_thread(self, cwd: str, owner_pid: int) -> Thread:
        return self._comms.claim_thread(
            self._thread_name_for(cwd),
            tags=frozenset({"acp"}),
            worktree=cwd,
            pid=owner_pid,
            start_at_latest=True,
            model=backend.configured_model(self._agent_args),
            thinking_level=backend.configured_thinking_level(self._agent_args),
            auto_title_pending=backend.rpc_args_for(self._agent_bin, self._agent_args) is not None,
        )

    @staticmethod
    def _reject_foreign_mcp(mcp_servers: list[Any] | None) -> None:
        # ACP declarations are not Pi package declarations. Silently accepting
        # them would misrepresent both the effective config and launch policy.
        if mcp_servers is not None and (type(mcp_servers) is not list or mcp_servers):
            raise RequestError.invalid_params(
                {
                    "reason": (
                        "ACP mcpServers are unsupported; use Pi's native MCP package configuration."
                    )
                }
            )

    async def new_session(
        self, cwd: str, mcp_servers: list[Any] | None = None, **kwargs: Any
    ) -> NewSessionResponse:
        self._reject_foreign_mcp(mcp_servers)
        thread = self._declare_thread(cwd, os.getpid())
        thread_name = thread.name
        session_id = thread_name
        self._sessions[session_id] = thread_name
        cursor, legacy = self._delivery_cursors.initialize(
            self._comms.registry.aliases_for(thread_name),
            thread_name,
            high_water=self._comms.message_high_water(),
            fresh=True,
        )
        self._inbox_cursors[session_id] = cursor
        self._legacy_through[session_id] = legacy
        self._session_titles[session_id] = thread_name
        self._session_worktrees[session_id] = thread.worktree
        if self._runtime_enabled:
            await self._runtime.start()
        self._ensure_live_drain(session_id)
        config_options = await self._config_options(thread_name)
        self._session_catalog_generation[session_id] = self._catalog_generation
        # The session response already carries these options; only an external
        # change should trigger a config update.
        current = self._comms.registry.require(thread.name)
        self._session_config_signature[session_id] = (
            current.model,
            current.thinking_level,
        )
        return NewSessionResponse(
            session_id=session_id,
            config_options=config_options,
            field_meta=self._session_metadata(thread_name),
        )

    def _validated_thread(self, cwd: str, session_id: str) -> Thread:
        thread = self._comms.registry.require(session_id)
        known_paths = {
            str(Path(path).expanduser().resolve())
            for path in (thread.worktree, *thread.previous_worktrees)
        }
        if str(Path(cwd).expanduser().resolve()) not in known_paths:
            raise RequestError.invalid_params(
                {"reason": "The saved thread belongs to a different working directory."}
            )
        return thread

    async def load_session(
        self,
        cwd: str,
        session_id: str,
        mcp_servers: list[Any] | None = None,
        **kwargs: Any,
    ) -> LoadSessionResponse:
        """Reconnect an ACP client to its persistent wire thread."""
        self._reject_foreign_mcp(mcp_servers)
        thread = self._validated_thread(cwd, session_id)
        thread = self._comms.acquire_thread(thread.name, owner_pid=os.getpid())
        if thread.pid != os.getpid():
            return await self._attach_owner(thread, session_id)
        self._comms.heartbeat(thread.name)
        self._sessions[session_id] = thread.name
        cursor, legacy = self._delivery_cursors.initialize(
            self._comms.registry.aliases_for(thread.name),
            thread.name,
            high_water=self._comms.message_high_water(),
            fresh=False,
        )
        self._inbox_cursors[session_id] = cursor
        self._legacy_through[session_id] = legacy
        self._session_titles[session_id] = thread.name
        self._session_worktrees[session_id] = thread.worktree
        if self._runtime_enabled:
            await self._runtime.start()
        await self._replay_transcript(session_id, thread.name)
        await self.replay_unknown_inputs(session_id)
        self._ensure_thread_model(thread.name)
        config_options = await self._config_options(thread.name)
        self._session_catalog_generation[session_id] = self._catalog_generation
        current = self._comms.registry.require(thread.name)
        self._session_config_signature[session_id] = (
            current.model,
            current.thinking_level,
        )
        self._ensure_live_drain(session_id)
        return LoadSessionResponse(
            config_options=config_options,
            field_meta=self._session_metadata(thread.name),
        )

    async def _attach_owner(self, thread: Thread, session_id: str) -> LoadSessionResponse:
        proxy = RuntimeProxy(self, session_id, socket_path(self._comms.root, thread.pid))
        try:
            metadata = await proxy.subscribe()
        except (OSError, RuntimeError) as error:
            await proxy.close()
            raise RequestError.invalid_params(
                {"reason": f"Unable to attach to {thread.name!r} owner {thread.pid}: {error}"}
            ) from error
        self._proxies[session_id] = proxy
        self._proxy_image_support[session_id] = (
            metadata.get("agentComms", {}).get("imagePrompts") is True
        )
        return LoadSessionResponse(
            config_options=metadata.pop("configOptions", []), field_meta=metadata
        )

    async def _replay_transcript(
        self,
        session_id: str,
        name: str,
        client: Any = None,
        *,
        snapshots: bool | None = None,
        diffs: bool | None = None,
    ) -> None:
        """Replay the negotiated client view or an explicitly selected internal view.

        None uses this attachment's capabilities. Explicit keyword overrides
        are trusted owner-side calls, not ACP request parameters; old clients
        never receive expanded snapshot fields merely by connecting.
        """
        use_snapshots = (
            (
                self._transcript_snapshots
                if client is None
                else getattr(client, "transcript_snapshots", False)
            )
            if snapshots is None
            else snapshots is True
        )
        if use_snapshots:
            page = await asyncio.to_thread(self._comms.thread_transcript_page, name)
            include_diff = (
                (
                    self._transcript_diffs
                    if client is None
                    else getattr(client, "transcript_diffs", False)
                )
                if diffs is None
                else diffs is True
            )
            await (client or self._runtime).session_update(
                session_id=session_id,
                update=AgentMessageChunk(
                    session_update="agent_message_chunk",
                    content=TextContentBlock(type="text", text=""),
                    field_meta={
                        "agentComms": {
                            "transcript": [
                                event.to_wire(include_diff=include_diff) for event in page.events
                            ],
                            "transcriptPage": page.metadata(),
                        }
                    },
                ),
            )
            return
        events = await asyncio.to_thread(self._comms.thread_transcript, name)
        for event in events:
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
                    "route": event.routing.reply if event.routing else None,
                },
                client=client,
            )

    async def prompt(self, session_id: str, prompt: list[Any], **kwargs: Any) -> PromptResponse:
        instructions = self._manual_compaction_instructions(prompt, kwargs)
        if instructions is not None:
            # Idle owner bridge alone owns the lock and the one-POST budget.
            if session_id in self._proxies:
                result = await self._proxies[session_id].request(
                    "compact", instructions=instructions
                )
            else:
                result = await self.compact_context(session_id, instructions)
            if not isinstance(result, dict) or result.get("ok") is not True:
                reason = (
                    result.get("error") if isinstance(result, dict) else None
                ) or "Compaction failed or is uncertain; not retried."
                raise RequestError(-32603, str(reason), {"reason": str(reason)})
            return PromptResponse(
                stop_reason="end_turn",
                field_meta={"agentComms": {"compaction": result}},
            )
        meta = kwargs.get("_meta") or kwargs.get("field_meta") or {}
        # The ACP SDK expands _meta entries into handler keyword arguments.
        options = kwargs.get("agentComms") or meta.get("agentComms", {})
        if not isinstance(options, dict):
            raise RequestError.invalid_params({"reason": "agentComms metadata must be an object"})
        meta = {**meta, "agentComms": options}
        delivery = options.get("delivery", "queue")
        if not isinstance(delivery, str) or delivery not in {"queue", "steer"}:
            raise RequestError.invalid_params({"reason": "delivery must be queue or steer"})
        display_text = options.get("userText") or self._prompt_text(prompt)
        defer_display = options.get("deferDisplay") is True
        if options.get("clearQueue") is True:
            # Attachment-only clients clear the owner's queue through the
            # existing prompt channel; no turn is launched.
            if session_id in self._proxies:
                await self._proxies[session_id].request("clear_queue")
            else:
                await self.clear_queued_inputs(session_id)
            return PromptResponse(stop_reason="end_turn")
        try:
            images = self._prompt_images(prompt)
        except ValueError as error:
            raise RequestError.invalid_params({"reason": str(error)}) from error
        if images:
            supported = (
                self._proxy_image_support.get(session_id, False)
                if session_id in self._proxies
                else backend.rpc_args_for(self._agent_bin, self._agent_args) is not None
            )
            if not supported:
                raise RequestError.invalid_params(
                    {"reason": "This owner does not support image prompts; refresh it while idle."}
                )
            if self._prompt_text(prompt).lstrip().startswith(("@", "#", RELAY_PREFIX)):
                raise RequestError.invalid_params(
                    {"reason": "Send images to an agent thread, not as a coordination relay."}
                )
        if session_id in self._proxies:
            result = await self._proxies[session_id].request(
                "prompt",
                meta=meta,
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
        if options.get("sendNow") is True:
            inbox = self._backend_inboxes.get(session_id)
            selected = list(self._queued_inputs.get(session_id, {}))
            if inbox is not None and selected:
                inbox.put_nowait({"type": "interrupt_steering", "_input_ids": selected})
            return PromptResponse(stop_reason="end_turn")
        text = self._prompt_text(prompt)
        if (
            session_id in self._active_turns
            and backend.rpc_args_for(self._agent_bin, self._agent_args) is not None
            and (inbox := self._backend_inboxes.get(session_id)) is not None
            and not text.lstrip().startswith(("@", "#", RELAY_PREFIX))
        ):
            pending_ids = self._forwarded_inputs.setdefault(session_id, set())
            if len(pending_ids) >= 32:
                raise RequestError.invalid_params(
                    {"reason": "Too many follow-up inputs awaiting their own user start."}
                )
            input_id = uuid4().hex
            pending_ids.add(input_id)
            key = f"acp:{input_id}"
            with _store_lock(self._comms._wire_lock_path):
                snapshot = self._comms.registry.snapshot()
                owner = snapshot.aliases.get(
                    self._require_session(session_id), self._require_session(session_id)
                )
                admission = snapshot.admission_generations[owner]
                admitted_goal = snapshot.threads[owner].goal
                self._steering_goal_ids.setdefault(session_id, {})[input_id] = (
                    admitted_goal.id if admitted_goal is not None and admitted_goal.active else None
                )
                self._dispositions.record(
                    key,
                    seq=None,
                    owner=owner,
                    admission=admission,
                    target=owner,
                    text=display_text or text or "[image prompt]",
                )
            self._steering_input_keys.setdefault(session_id, {})[input_id] = key
            self._turn_input_keys.setdefault(session_id, set()).add(key)
            try:
                if delivery == "queue":
                    self._queued_inputs.setdefault(session_id, {})[input_id] = QueuedInput(
                        display_text, defer_display
                    )
                    await self._emit_queue_state(session_id)
                inbox.put_nowait(
                    {
                        "type": "prompt",
                        "message": "User follow-up:\n" + text.removeprefix(AGENT_PREFIX),
                        # Queue once; explicit immediate delivery interrupts the
                        # current response after this exact input is accepted.
                        "streamingBehavior": "steer",
                        "_input_id": input_id,
                        **({"images": [image.to_rpc() for image in images]} if images else {}),
                    }
                )
                if delivery == "steer":
                    inbox.put_nowait({"type": "interrupt_steering", "_input_ids": [input_id]})
            except BaseException:
                self._queued_inputs.get(session_id, {}).pop(input_id, None)
                raise
            # ACP receipt is only local acceptance. The matching inputStarted
            # update, not this end_turn, is the model-read boundary.
            return PromptResponse(
                stop_reason="end_turn",
                field_meta={
                    "agentComms": {
                        "inputDisposition": {
                            "inputId": input_id,
                            "status": "accepted_not_started",
                            "delivery": delivery,
                        }
                    }
                },
            )
        async with self._turn_locks.setdefault(session_id, asyncio.Lock()):
            # A direct ACP prompt owns this one controller. Owner-socket prompts
            # already carry a private subscriber binding (including None when
            # absent); never fall back to a passive ACP client in that case.
            existing = self._runtime.controller.get()
            context = (
                self._runtime.controller.set(self._client)
                if existing is UNBOUND_CONTROLLER
                else None
            )
            try:
                return await self._prompt_owned(
                    session_id, prompt, display_text=display_text if defer_display else None
                )
            finally:
                if context is not None:
                    self._runtime.controller.reset(context)

    @staticmethod
    def _prompt_images(prompt: list[Any]) -> tuple[Any, ...]:
        """Defer optional image support; never flatten unsupported blocks to text."""
        try:
            from .image_inputs import prompt_images
        except ImportError as error:
            if all(
                (block.get("type") if isinstance(block, dict) else getattr(block, "type", None))
                == "text"
                for block in prompt
            ):
                return ()
            raise RequestError.invalid_params(
                {"reason": "Image/resource prompts require reviewed image support."}
            ) from error
        return prompt_images(prompt)

    @staticmethod
    def _manual_compaction_instructions(prompt: list[Any], kwargs: dict[str, Any]) -> str | None:
        # Toad sends one blank text block with _meta.agentComms.compact;
        # other ACP clients can send literal /compact. Never flatten images.
        metadata = kwargs.get("agentComms")
        if metadata is None:
            container = kwargs.get("field_meta") or kwargs.get("_meta") or {}
            metadata = container.get("agentComms") if isinstance(container, dict) else None
        has_metadata_command = isinstance(metadata, dict) and "compact" in metadata
        text = CommsAgent._prompt_text(prompt).strip()
        if not has_metadata_command and re.match(r"^/compact(?:\s|$)", text) is None:
            return None
        if len(prompt) != 1:
            raise RequestError.invalid_params({"reason": "/compact requires one text block."})
        block = prompt[0]
        kind = block.get("type") if isinstance(block, dict) else getattr(block, "type", None)
        if kind != "text":
            raise RequestError.invalid_params({"reason": "/compact requires text only."})
        if has_metadata_command:
            if text:
                raise RequestError.invalid_params(
                    {"reason": "Compaction metadata requires a blank text block."}
                )
            value = cast(dict[str, Any], metadata)["compact"]
            if value is not None and not isinstance(value, str):
                raise RequestError.invalid_params({"reason": "Invalid compaction instructions."})
            instructions = (value or "").strip()
        else:
            instructions = text[len("/compact") :].strip()
        if len(instructions) > 2000:
            raise RequestError.invalid_params({"reason": "Compaction instructions are too long."})
        return instructions

    async def clear_queued_inputs(self, session_id: str) -> None:
        """Drop prompts queued against the backend for this session.

        Pi's own queue is cleared, then the local deferred-display list is
        reset. Callers may re-send the prompts they want to keep.
        """
        inbox = self._backend_inboxes.get(session_id)
        if inbox is not None:
            inbox.put_nowait({"type": "clear_queue"})
        self._queued_inputs.pop(session_id, None)
        await self._emit_queue_state(session_id)

    async def compact_context(
        self, session_id: str, instructions: str | None = None
    ) -> dict[str, Any]:
        """Use the idle owner bridge; never Pi's implicit retrying compaction."""
        from .manual_compaction_bridge import compact_context

        return await compact_context(self, session_id, instructions)

    async def _emit_queue_state(
        self, session_id: str, *, restored: list[str] | None = None, client: Any = None
    ) -> None:
        await (client or self._runtime).session_update(
            session_id=session_id,
            update=AgentMessageChunk(
                session_update="agent_message_chunk",
                content=TextContentBlock(type="text", text=""),
                field_meta={
                    "agentComms": {
                        "queue": [
                            item.text for item in self._queued_inputs.get(session_id, {}).values()
                        ],
                        "restored": restored or [],
                    }
                },
            ),
        )

    async def _emit_input_started(
        self, session_id: str, text: str | None, input_id: str | None = None
    ) -> None:
        proof: dict[str, Any] = {"text": text}
        if input_id is not None:
            proof["inputId"] = input_id
        await self._runtime.session_update(
            session_id=session_id,
            update=AgentMessageChunk(
                session_update="agent_message_chunk",
                content=TextContentBlock(type="text", text=""),
                field_meta={"agentComms": {"inputStarted": proof}},
            ),
        )

    async def _emit_input_disposition(
        self, session_id: str, row: dict[str, Any], client: Any = None
    ) -> None:
        await self._emit_public_input_disposition(session_id, InputDispositions.public(row), client)

    async def _emit_public_input_disposition(
        self, session_id: str, disposition: dict[str, Any], client: Any = None
    ) -> None:
        await (client or self._runtime).session_update(
            session_id=session_id,
            update=AgentMessageChunk(
                session_update="agent_message_chunk",
                content=TextContentBlock(type="text", text=""),
                field_meta={"agentComms": {"inputDisposition": disposition}},
            ),
        )

    async def emit_input_delivery_changed(self, session_id: str) -> None:
        """Invalidate attached views after a notice-only owner action."""
        await self._runtime.session_update(
            session_id=session_id,
            update=AgentMessageChunk(
                session_update="agent_message_chunk",
                content=TextContentBlock(type="text", text=""),
                field_meta={"agentComms": {"inputDeliveryChanged": True}},
            ),
        )

    async def replay_unknown_inputs(self, session_id: str, client: Any = None) -> None:
        owner = self._require_session(session_id)
        overview = self._comms.input_delivery(owner)
        for disposition in overview["inputs"]:
            await self._emit_public_input_disposition(session_id, disposition, client=client)

    async def emit_session_identity(self, session_id: str, name: str, client: Any = None) -> None:
        """Let a subscriber identify its owner before potentially long replay."""
        await (client or self._runtime).session_update(
            session_id=session_id,
            update=AgentMessageChunk(
                session_update="agent_message_chunk",
                content=TextContentBlock(type="text", text=""),
                field_meta=self._session_metadata(name),
            ),
        )

    async def _prompt_owned(
        self, session_id: str, prompt: list[Any], *, display_text: str | None = None
    ) -> PromptResponse:
        turn_task = asyncio.current_task()
        assert turn_task is not None
        self._turn_tasks[session_id] = turn_task
        try:
            thread_name = await self._sync_session_identity(session_id)
            self._comms.registry.require(thread_name)
            text = self._prompt_text(prompt)
            images = self._prompt_images(prompt)
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
            if images:
                await self._run_owned_input(
                    session_id,
                    thread_name,
                    agent_task or "",
                    images=images,
                    display_text=display_text,
                )
            elif agent_task:
                await self._run_owned_input(
                    session_id, thread_name, agent_task, display_text=display_text
                )
            else:
                turn_id = uuid4().hex
                self._comms.begin_turn(thread_name, turn_id, "Waiting for replies")
                self._active_turns[session_id] = turn_id
                try:
                    await self._emit_event(session_id, self._started_event(thread_name, turn_id))
                    await self._drain_inbox(session_id)
                    await self._collect_replies(session_id, thread_name, sent_seq)
                finally:
                    self._active_turns.pop(session_id, None)
                    self._comms.finish_turn(thread_name, turn_id)
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

    async def _run_owned_input(
        self,
        session_id: str,
        thread_name: str,
        task: str,
        *,
        images: tuple[Any, ...] = (),
        display_text: str | None = None,
    ) -> None:
        if backend.rpc_args_for(self._agent_bin, self._agent_args) is None:
            # The plain text fallback has no Pi native input-ID protocol.
            # Preserve its existing local command behavior without attaching
            # a false Pi start claim to it.
            await self._run_agent_turn(session_id, thread_name, task, images=images)
            return
        key = f"acp:{uuid4().hex}"
        with _store_lock(self._comms._wire_lock_path):
            snapshot = self._comms.registry.snapshot()
            canonical = snapshot.aliases.get(thread_name, thread_name)
            admitted_goal = snapshot.threads[canonical].goal
            original_goal_id = (
                admitted_goal.id if admitted_goal is not None and admitted_goal.active else None
            )
            self._dispositions.record(
                key,
                seq=None,
                owner=canonical,
                admission=snapshot.admission_generations[canonical],
                target=canonical,
                text=display_text or task or "[image prompt]",
            )
        row = self._dispositions.get(key)
        assert row is not None
        await self._emit_input_disposition(session_id, row)
        await self._run_agent_turn(
            session_id,
            thread_name,
            task,
            images=images,
            original_keys=(key,),
            initial_display_text=display_text,
            original_owner_input=True,
            original_goal_id=original_goal_id,
        )

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
            if name != thread_name and activity.state.busy:
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
        name = self._sessions.get(session_id)
        if name and (goal := self._comms.registry.require(name).goal) and goal.active:
            self._comms.update_goal(name, "paused", goal_id=goal.id, owner_action=True)
        task = self._turn_tasks.get(session_id)
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            await backend.terminate_task_process(task)
        thread_name = self._sessions.get(session_id)
        if thread_name:
            self._comms.acknowledge(thread_name)

    async def set_config_option(
        self, config_id: str, session_id: str, value: str | bool, **kwargs: Any
    ) -> SetSessionConfigOptionResponse:
        if config_id not in {"model", "thinking_level"} or not isinstance(value, str):
            raise RequestError.invalid_params(
                {"reason": f"Unknown session configuration option: {config_id!r}"}
            )
        if session_id in self._proxies:
            result = await self._proxies[session_id].request(
                "set_config_option", config_id=config_id, value=value
            )
            return SetSessionConfigOptionResponse.model_validate(result)
        thread_name = await self._sync_session_identity(session_id)
        thread = self._comms.registry.require(thread_name)
        if config_id == "model":
            choices = {model.id for model in await self._models_for(thread_name)}
            if value not in choices:
                raise RequestError.invalid_params({"reason": f"Unknown model: {value!r}"})
            await self._set_active_backend_option(
                session_id,
                {
                    "type": "set_model",
                    "provider": value.split("/", 1)[0],
                    "modelId": value.split("/", 1)[1],
                },
                self._model_requests,
                "Model change timed out",
            )
            self._comms.set_thread_model(thread_name, value)
            levels = await self._thinking_levels_for(value)
            if thread.thinking_level not in levels:
                self._comms.set_thread_thinking_level(
                    thread_name, "medium" if "medium" in levels else levels[0]
                )
        else:
            levels = await self._thinking_levels_for(thread.model)
            if value not in levels:
                raise RequestError.invalid_params(
                    {"reason": f"Thinking level {value!r} is unavailable for this model"}
                )
            await self._set_active_backend_option(
                session_id,
                {"type": "set_thinking_level", "level": value},
                self._thinking_requests,
                "Thinking level change timed out",
            )
            self._comms.set_thread_thinking_level(thread_name, value)
        if session_id not in self._active_turns and (
            persistent := self._persistent_backends.get(session_id)
        ):
            await persistent.close_idle()
        config_options = await self._config_options(thread_name)
        await self._runtime.session_update(
            session_id=session_id,
            update=ConfigOptionUpdate(
                session_update="config_option_update", config_options=config_options
            ),
        )
        return SetSessionConfigOptionResponse(config_options=config_options)

    async def _set_active_backend_option(
        self,
        session_id: str,
        command: dict[str, Any],
        requests: dict[str, asyncio.Future[None]],
        timeout_message: str,
    ) -> None:
        if session_id not in self._active_turns or not (
            inbox := self._backend_inboxes.get(session_id)
        ):
            return
        request_id = uuid4().hex
        future = asyncio.get_running_loop().create_future()
        requests[request_id] = future
        inbox.put_nowait({"id": request_id, **command})
        try:
            await asyncio.wait_for(future, timeout=10)
        except (TimeoutError, RuntimeError) as error:
            raise RequestError.invalid_params({"reason": str(error) or timeout_message}) from error
        finally:
            requests.pop(request_id, None)

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
        thread = self._comms.registry.require(thread_name)
        goal, execution = self._comms.goal_snapshot(thread_name)
        info = self._comms.agent_info_of(thread_name)
        usage = (
            {"used": info.context_used, "size": info.context_size, "source": "last_response"}
            if info is not None and info.context_used is not None and info.context_size
            else None
        )
        return {
            "agentComms": {
                "thread": thread_name,
                "goal": asdict(goal) if goal else None,
                "goalExecution": asdict(execution) if execution else None,
                "wireRoot": str(self._comms.root.resolve()),
                "persistence": "shared on-disk wire",
                "transport": "per-session stdio ACP",
                "ownerPid": os.getpid(),
                "contextUsage": usage,
                "turnLifecycle": True,
                "model": thread.model,
                "thinkingLevel": thread.thinking_level,
                "worktree": thread.worktree,
                "autoTitle": backend.rpc_args_for(self._agent_bin, self._agent_args) is not None,
                "title": thread.title or thread.name,
                "promptQueue": backend.rpc_args_for(self._agent_bin, self._agent_args) is not None,
                "imagePrompts": backend.rpc_args_for(self._agent_bin, self._agent_args) is not None,
            }
        }

    def _ensure_thread_model(self, thread_name: str) -> str | None:
        thread = self._comms.registry.require(thread_name)
        selected = thread.model
        if selected is None:
            selected = self._comms.resolve_thread_model(
                thread.name, backend.configured_model(self._agent_args)
            )
        if selected is not None and selected != thread.model:
            self._comms.set_thread_model(thread.name, selected)
        return selected

    async def _models_for(self, thread_name: str) -> list[backend.Model]:
        selected = self._ensure_thread_model(thread_name)
        async with self._model_catalog_lock:
            if self._model_catalog is None or self._model_catalog_auth != backend.auth_revision():
                self._model_catalog = await backend.discover_models(
                    self._agent_bin, self._agent_args, selected
                )
                self._model_catalog_auth = backend.auth_revision()
                self._catalog_generation += 1
        if selected and all(model.id != selected for model in self._model_catalog):
            return [backend.Model(selected, selected), *self._model_catalog]
        return self._model_catalog

    async def _refresh_auth_models(self) -> None:
        if self._model_catalog is None:
            return
        if self._model_catalog_auth == backend.auth_revision() and all(
            self._session_catalog_generation.get(sid) == self._catalog_generation
            for sid in self._sessions
        ):
            return
        async with self._catalog_publish_lock:
            for sid, name in tuple(self._sessions.items()):
                options = await self._config_options(name)
                if self._session_catalog_generation.get(sid) == self._catalog_generation:
                    continue
                await self._runtime.session_update(
                    session_id=sid,
                    update=ConfigOptionUpdate(
                        session_update="config_option_update", config_options=options
                    ),
                )
                self._session_catalog_generation[sid] = self._catalog_generation

    async def _config_options(self, thread_name: str) -> list[Any]:
        selected = self._ensure_thread_model(thread_name)
        if selected is None:
            return []
        models = await self._models_for(thread_name)
        levels = await self._thinking_levels_for(selected)
        thread = self._comms.registry.require(thread_name)
        thinking_level = thread.thinking_level
        if thinking_level not in levels:
            thinking_level = "medium" if "medium" in levels else levels[0]
            self._comms.set_thread_thinking_level(thread_name, thinking_level)
        return [
            SessionConfigOptionSelect(
                id="model",
                name="Model",
                description="Model used by this persistent agent thread",
                category="model",
                type="select",
                current_value=selected,
                options=[
                    SessionConfigSelectOption(
                        value=model.id,
                        name=model.name,
                        description=model.description,
                    )
                    for model in models
                ],
            ),
            SessionConfigOptionSelect(
                id="thinking_level",
                name="Thinking level",
                description="Reasoning effort used by this persistent agent thread",
                category="thought_level",
                type="select",
                current_value=thinking_level,
                options=[
                    SessionConfigSelectOption(value=level, name=level.title()) for level in levels
                ],
            ),
        ]

    async def _thinking_levels_for(self, model: str | None) -> list[str]:
        key = (model, backend.auth_revision())
        if key not in self._thinking_catalog:
            self._thinking_catalog[key] = await backend.discover_thinking_levels(
                self._agent_bin, self._agent_args, model
            )
        return self._thinking_catalog[key]

    async def _sync_session_identity(self, session_id: str) -> str:
        """Follow permanent aliases and publish the canonical wire name."""
        cached_name = self._require_session(session_id)
        thread = self._comms.registry.require(cached_name)
        thread_name = thread.name
        if (
            session_id not in self._active_turns
            and (persistent := self._persistent_backends.get(session_id))
            and (
                thread_name != cached_name
                or thread.pid != os.getpid()
                or not self._comms.registry.status(thread_name).running
            )
        ):
            await persistent.close_idle()
        self._sessions[session_id] = thread_name
        if (
            self._session_titles.get(session_id) != thread_name
            or self._display_titles.get(session_id) != thread.title
        ):
            await self._runtime.session_update(
                session_id=session_id,
                update=SessionInfoUpdate(
                    session_update="session_info_update",
                    title=thread.title or thread_name,
                    field_meta={"agentComms": {"thread": thread_name}},
                ),
            )
            self._session_titles[session_id] = thread_name
            self._display_titles[session_id] = thread.title
        if self._session_worktrees.get(session_id) != thread.worktree:
            await self._runtime.session_update(
                session_id=session_id,
                update=SessionInfoUpdate(
                    session_update="session_info_update",
                    field_meta={"agentComms": {"worktree": thread.worktree}},
                ),
            )
            self._session_worktrees[session_id] = thread.worktree
        return thread_name

    def _ensure_live_drain(self, session_id: str) -> None:
        """Keep one background task per session pushing inbox messages live."""
        if self._closing:
            return
        if session_id in self._drain_tasks and not self._drain_tasks[session_id].done():
            return

        async def loop() -> None:
            watcher = open_wire_watcher(self._comms.root)
            try:
                while True:
                    if watcher is None:
                        await asyncio.sleep(LIVE_DRAIN_INTERVAL)
                    else:
                        watcher.changed.clear()
                    try:
                        await self._drain_inbox(session_id)
                        await self._sync_thread_config(session_id)
                        self._schedule_goal(session_id)
                        await self._refresh_auth_models()
                    except asyncio.CancelledError:
                        raise
                    except Exception as error:
                        # Never let the drain task die silently: a dead drain
                        # means replies stop reaching the client.
                        self._debug_log(f"live-drain error: {error!r}")
                    if watcher is not None:
                        if watcher.invalid:
                            watcher.close()
                            watcher = None
                        else:
                            with suppress(TimeoutError):
                                async with asyncio.timeout(WATCH_FALLBACK_INTERVAL):
                                    await watcher.changed.wait()
            finally:
                if watcher is not None:
                    watcher.close()

        context = self._runtime.controller.set(UNBOUND_CONTROLLER)
        try:
            task = asyncio.create_task(loop())
        finally:
            self._runtime.controller.reset(context)
        self._drain_tasks[session_id] = task

    async def _drain_inbox(self, session_id: str) -> int:
        """Push undelivered messages to the client; returns count pushed."""
        async with self._drain_locks.setdefault(session_id, asyncio.Lock()):
            return await self._drain_owned_inbox(session_id)

    async def _sync_thread_config(self, session_id: str) -> None:
        """Publish a thread's model/thinking when another client changes it.

        An agent can change another thread's model with ``comms_model``. The
        owning process must then tell its connected clients, or their model
        switcher keeps showing the previous selection.
        """
        thread_name = await self._sync_session_identity(session_id)
        await self._sync_goal_execution(session_id, thread_name)
        thread = self._comms.registry.require(thread_name)
        signature = (thread.model, thread.thinking_level)
        if self._session_config_signature.get(session_id) == signature:
            return
        self._session_config_signature[session_id] = signature
        if self._client is None and not self._runtime_enabled:
            return
        options = await self._config_options(thread_name)
        await self._runtime.session_update(
            session_id=session_id,
            update=ConfigOptionUpdate(
                session_update="config_option_update", config_options=options
            ),
        )

    async def _sync_goal_execution(self, session_id: str, thread_name: str) -> None:
        goal, execution = self._comms.goal_snapshot(thread_name)
        signature = (goal, execution)
        previous = self._goal_execution_signatures
        if session_id in previous and previous[session_id] == signature:
            return
        await self._runtime.session_update(
            session_id=session_id,
            update=SessionInfoUpdate(
                session_update="session_info_update",
                field_meta={
                    "agentComms": {
                        "goal": asdict(goal) if goal else None,
                        "goalExecution": asdict(execution) if execution else None,
                    }
                },
            ),
        )
        previous[session_id] = signature

    async def _drain_owned_inbox(self, session_id: str) -> int:
        thread_name = await self._sync_session_identity(session_id)
        if self._comms.registry.status(thread_name).stopped:
            return 0
        backend_inbox = self._backend_inboxes.get(session_id)
        if self._client is None and backend_inbox is None and not self._runtime_enabled:
            return 0
        pushed = 0
        after = self._inbox_cursors.get(session_id, 0)
        high_water = self._comms.message_high_water()
        page = self._comms.incoming_page(thread_name, after=after) if after < high_water else None
        incoming_messages = page.messages if page else ()
        for message in incoming_messages:
            admitted = True
            dependency_wait = None
            row: dict[str, Any] | None = None
            with _store_lock(self._comms._wire_lock_path):
                snapshot = self._comms.registry.snapshot()
                current_name = snapshot.aliases.get(thread_name, thread_name)
                current = snapshot.threads[current_name]
                status = snapshot.statuses[current_name]
                aliases = frozenset(
                    {
                        current_name,
                        *(
                            alias
                            for alias, target in snapshot.aliases.items()
                            if target == current_name
                        ),
                    }
                )
                incoming = ScheduledTurn.incoming(message, aliases=snapshot.aliases)
                starts_turn = message.starts_turn_for(current_name, aliases=snapshot.aliases)
                direct = starts_turn and message.target in aliases
                if starts_turn:
                    wait = self._comms.goal_wait(current_name) if direct else None
                    if wait is not None and wait.matches(message, snapshot):
                        dependency_wait = wait
                        incoming = replace(
                            incoming, goal_id=wait.goal_id, goal_wait_id=wait.wait_id
                        )
                    key = self._dispositions.bus_key(message, current)
                    admitted = self._dispositions.record(
                        key,
                        seq=message.seq,
                        owner=current_name,
                        admission=snapshot.admission_generations[current_name],
                        target=message.target,
                        text=incoming.prompt,
                    )
                    row = self._dispositions.get(key)
                    admitted = (
                        admitted
                        and message.seq > self._legacy_through.get(session_id, 0)
                        and status.running
                        and (
                            current.goal is None
                            or not current.goal.active
                            or dependency_wait is not None
                        )
                        and backend.rpc_args_for(self._agent_bin, self._agent_args) is not None
                    )
                self._delivery_cursors.advance(aliases, message.seq)
                self._inbox_cursors[session_id] = message.seq
            if row is not None and row["status"] == "unknown":
                await self._emit_input_disposition(session_id, row)
            if (
                admitted
                and dependency_wait is None
                and backend_inbox is not None
                and starts_turn
                and incoming.reply_target is None
            ):
                input_id = f"bus-{message.seq}"
                self._steering_origins.setdefault(session_id, {})[input_id] = message
                self._steering_input_keys.setdefault(session_id, {})[input_id] = key
                self._turn_input_keys.setdefault(session_id, set()).add(key)
                self._forwarded_inputs.setdefault(session_id, set()).add(input_id)
                backend_inbox.put_nowait(
                    {
                        "type": "prompt",
                        "message": incoming.prompt,
                        "streamingBehavior": "steer",
                        "_input_id": input_id,
                    }
                )
            elif admitted and self._auto_wake and self._runtime_enabled and starts_turn:
                self._pending_turns.setdefault(session_id, []).append(incoming)
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
            pushed += 1
        if page is not None and not page.has_newer:
            aliases = self._comms.registry.aliases_for(thread_name)
            self._delivery_cursors.advance(aliases, high_water)
            self._inbox_cursors[session_id] = max(
                self._inbox_cursors.get(session_id, 0), high_water
            )
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
                    owner = self._comms.registry.require(self._require_session(session_id))
                    if not self._comms.registry.status(owner.name).running:
                        # The durable UNKNOWN rows remain visible. A stopped
                        # owner cannot launch a turn from this old wake queue.
                        continue
                    goal = owner.goal
                    if goal is not None and goal.active:
                        pending = [turn for turn in pending if turn.goal_id == goal.id]
                    else:
                        pending = [turn for turn in pending if turn.goal_id is None]
                    if not pending:
                        continue
                    pending, remaining = ScheduledTurn.take_batch(pending)
                    if remaining:
                        self._pending_turns[session_id] = remaining
                    self._turn_tasks[session_id] = asyncio.current_task()  # type: ignore[assignment]
                    try:
                        await self._run_agent_turn(
                            session_id,
                            self._require_session(session_id),
                            "\n\n".join(turn.prompt for turn in pending),
                            reply_targets=tuple(
                                dict.fromkeys(
                                    turn.reply_target for turn in pending if turn.reply_target
                                )
                            ),
                            origins=tuple(
                                turn.origin for turn in pending if turn.origin is not None
                            ),
                            autonomous_goal=bool(
                                len(pending) == 1
                                and pending[0].goal_id is not None
                                and pending[0].origin is None
                            ),
                            dependency_wait_id=pending[0].goal_wait_id,
                        )
                    except RequestError:
                        if pending[0].goal_wait_id is None or goal is None:
                            raise
                        self._comms.block_goal_after_failed_turn(
                            owner.name,
                            started_goal=goal,
                            expected_worktree=owner.worktree,
                            diagnostic=(
                                "Standby wake launch authority unavailable; inspect the UNKNOWN "
                                "input before explicit Retry. No input was replayed."
                            ),
                        )
                        await self._sync_goal_execution(session_id, owner.name)
                    finally:
                        self._turn_tasks.pop(session_id, None)

        # Background work must not inherit a human controller from the task
        # that happened to schedule it. Only its own ACP prompt may bind one.
        context = self._runtime.controller.set(UNBOUND_CONTROLLER)
        try:
            self._wake_tasks[session_id] = asyncio.create_task(wake())
        finally:
            self._runtime.controller.reset(context)

    def _schedule_goal(self, session_id: str) -> None:
        """Only the existing thread owner may schedule another goal turn."""
        if (
            self._closing
            or session_id in self._turn_tasks
            or session_id in self._backend_inboxes
            or session_id in self._active_turns
            or self._pending_turns.get(session_id)
        ):
            return
        if (wake := self._wake_tasks.get(session_id)) is not None and not wake.done():
            return
        thread = self._comms.registry.require(self._require_session(session_id))
        if thread.pid != os.getpid() or not self._comms.registry.status(thread.name).running:
            return
        goal = thread.goal
        if goal is not None and goal.active:
            if self._comms.goal_wait(thread.name) is not None:
                return
            if self._pending_goal_origins.get(thread.name) == goal.id:
                return
            store = self._goal_store
            if (
                store is None
                and (self._comms.root / "goal-private" / "goal_attempts.sqlite3").exists()
            ):
                store = self._open_goal_store()
            if store is None:
                self._comms.block_goal_after_failed_turn(
                    thread.name,
                    started_goal=goal,
                    expected_worktree=thread.worktree,
                    diagnostic="Goal launch grant unavailable; explicit Retry required.",
                )
                return
            try:
                generation = store.snapshot(goal.id)
                if generation is None or generation.state != "ready":
                    self._comms.block_goal_after_failed_turn(
                        thread.name,
                        started_goal=goal,
                        expected_worktree=thread.worktree,
                        diagnostic="Goal attempt unresolved; inspect diagnostics before Retry.",
                    )
                    return
                admission = self._comms.registry.snapshot().admission_generations[thread.name]
                with _store_lock(self._comms._wire_lock_path):
                    self._ready_goal_grant_locked(thread, admission, store, generation)
            except StaleAttempt:
                return
            except GoalAttemptError:
                self._comms.block_goal_after_failed_turn(
                    thread.name,
                    started_goal=goal,
                    expected_worktree=thread.worktree,
                    diagnostic="Goal launch grant unavailable; explicit Retry required.",
                )
                return
            self._pending_turns.setdefault(session_id, []).append(
                ScheduledTurn(GOAL_CONTINUE_PROMPT, goal_id=goal.id)
            )
            self._schedule_wake(session_id)

    def _open_goal_store(self) -> GoalAttemptStore:
        if self._goal_store is None:
            private = self._comms.root / "goal-private"
            private.mkdir(mode=0o700, exist_ok=True)
            self._goal_store = GoalAttemptStore.initialize(private)
        return self._goal_store

    def _ready_goal_grant_locked(
        self, owner: Thread, admission: int, store: GoalAttemptStore, generation: Generation
    ) -> str:
        """Caller holds the wire lock; READY recovery never authorizes an old owner."""
        snapshot = self._comms.registry.snapshot()
        name = snapshot.aliases.get(owner.name, owner.name)
        current = snapshot.threads.get(name)
        if (
            current is None
            or current.pid != os.getpid()
            or current.pid != owner.pid
            or current.created_at != owner.created_at
            or current.worktree != owner.worktree
            or not snapshot.statuses[name].running
            or snapshot.admission_generations[name] != admission
            or current.goal is None
            or not current.goal.active
            or current.goal.id != generation.goal_id
        ):
            raise StaleAttempt("The executing goal owner changed before READY recovery.")
        try:
            return store.ready_grant(generation.goal_id, generation.number)
        except UnresolvedAttempt:
            store.recover_unreserved_ready(generation.goal_id, generation.number)
            return store.ready_grant(generation.goal_id, generation.number)

    async def set_goal(self, session_id: str, text: str) -> Goal:
        """Commit a UI goal through its executing owner and private launch ledger."""
        if not isinstance(text, str) or not text.strip():
            raise ValueError("A goal requires text.")
        if backend.rpc_args_for(self._agent_bin, self._agent_args) is None:
            raise ValueError("Persistent goals require a native Pi backend.")
        name = self._require_session(session_id)
        goal = self._comms.update_goal(
            name,
            "set",
            text=text,
            owner_store=self._open_goal_store(),
            expected_owner_pid=os.getpid(),
        )
        assert goal is not None
        self._schedule_goal(session_id)
        return goal

    async def edit_goal(
        self, session_id: str, goal_id: str, expected_revision: int, text: str
    ) -> Goal:
        """Edit the current objective without replacing its identity or execution state."""
        name = self._require_session(session_id)
        goal = self._comms.registry.require(name).goal
        if goal is None or goal.id != goal_id or goal.revision != expected_revision:
            raise ValueError("The goal changed; refresh its state before editing.")
        # update_goal owns the wire lock and atomically rechecks both this
        # snapshot and the executing owner. Do not acquire its lock twice.
        edited = self._comms.update_goal(
            name,
            "edit",
            text=text,
            goal_id=goal_id,
            expected_goal=goal,
            expected_owner_pid=os.getpid(),
        )
        assert edited is not None
        await self._sync_thread_config(session_id)
        return edited

    async def update_goal(
        self, session_id: str, status: str, goal_id: str, expected_revision: int
    ) -> Goal | None:
        """Apply an explicit UI pause, resume, or clear through the current owner."""
        if status not in {"active", "paused", "clear"}:
            raise ValueError("Goal updates support only active, paused, or clear.")
        name = self._require_session(session_id)
        goal = self._comms.registry.require(name).goal
        if goal is None or goal.id != goal_id or goal.revision != expected_revision:
            raise ValueError("The goal changed; refresh its state before updating.")
        updated = self._comms.update_goal(
            name,
            status,
            goal_id=goal_id,
            expected_goal=goal,
            expected_owner_pid=os.getpid(),
            owner_action=True,
        )
        if status == "active":
            self._schedule_goal(session_id)
        await self._sync_thread_config(session_id)
        return updated

    async def retry_goal(self, session_id: str, goal_id: str, expected_revision: int) -> Goal:
        """Record an explicit UI retry in the executing owner's private ledger."""
        name = self._require_session(session_id)
        with _store_lock(self._comms._wire_lock_path):
            thread = self._comms.registry.require(name)
            if thread.pid != os.getpid() or not self._comms.registry.status(name).running:
                raise ValueError("The goal owner changed; refresh its state.")
            goal = thread.goal
            if (
                goal is None
                or goal.id != goal_id
                or goal.revision != expected_revision
                or goal.status != "blocked"
            ):
                raise ValueError("The blocked goal changed; refresh its state.")
            if self._pending_goal_origins.get(name) == goal_id:
                raise ValueError("Wait for the goal origin turn to finish.")
            resumed = replace(goal, status="active", revision=goal.revision + 1)
            store = self._open_goal_store()
            generation = store.snapshot(goal_id)
            if generation is None:
                # Older UI clients wrote only the registry goal. This explicit
                # human Retry may create the missing ledger; it never silently
                # replays a prior unknown provider attempt.
                store.create_goal(goal_id)
                generation = store.snapshot(goal_id)
                assert generation is not None
            if generation.state == "blocked" and generation.attempt_id:
                store.authorize_retry(
                    goal_id,
                    expected_generation=generation.number,
                    attempt_id=generation.attempt_id,
                    user_decision_id=uuid4().hex,
                )
            elif generation.state == "ready" and generation.attempt_id is None:
                # A previous explicit retry may have durably created READY
                # before the registry update, then crashed with its grant.
                store.authorize_ready_recovery(
                    goal_id,
                    expected_generation=generation.number,
                    user_decision_id=uuid4().hex,
                )
            else:
                raise ValueError("The goal attempt is unresolved; inspect it before retrying.")
            self._comms.registry.register(
                replace(thread, goal=resumed), self._comms.registry.status(name)
            )
        # READY records the accepted owner decision even during an unrelated
        # turn. The scheduler's existing busy fences defer launch until that
        # turn finishes; reserved/claimed attempts remain unretryable above.
        self._schedule_goal(session_id)
        await self._sync_goal_execution(session_id, name)
        return resumed

    async def _drain_count(self, session_id: str) -> int:
        return await self._drain_inbox(session_id)

    async def _extension_ui_permission(
        self,
        session_id: str,
        turn_id: str,
        controller: Any,
        request: dict[str, Any],
    ) -> dict[str, Any] | None:
        """Project one bounded Pi UI dialog to exactly the turn's ACP controller.

        No ACP response updates package configuration, launch trust or call grants.
        The backend revalidates this result before replying to the same Pi child.
        """
        if self._active_turns.get(session_id) != turn_id or controller is None:
            return None
        title, method = request.get("title"), request.get("method")
        if type(title) is not str or not title or len(title) > 160:
            return None
        choices: dict[str, str] = {}
        if method == "confirm":
            body = request.get("message")
            if type(body) is not str or len(body) > 8192:
                return None
            options = [
                PermissionOption(option_id="allow-once", name="Allow once", kind="allow_once"),
                PermissionOption(option_id="deny", name="Deny", kind="reject_once"),
            ]
        elif method == "select":
            values = request.get("options")
            if (
                type(values) is not list
                or not 1 <= len(values) <= 8
                or any(type(item) is not str or not item or len(item) > 100 for item in values)
            ):
                return None
            choices = {f"choice-{index}": value for index, value in enumerate(values)}
            options = [
                PermissionOption(option_id=key, name=f"Choose {value}", kind="allow_once")
                for key, value in choices.items()
            ]
            options.append(PermissionOption(option_id="deny", name="Cancel", kind="reject_once"))
            body = "Select one Pi extension option for this turn only."
        else:
            return None
        tool_call = ToolCallUpdate(
            tool_call_id=f"pi-ui-{turn_id}-{request['id']}",
            kind="other",
            title=title,
            content=[
                ContentToolCallContent(
                    type="content", content=TextContentBlock(type="text", text=body)
                )
            ],
        )
        try:
            if isinstance(controller, SocketClient):
                reply = await self._runtime.request_permission(
                    session_id,
                    controller,
                    {
                        "toolCall": tool_call.model_dump(by_alias=True, exclude_none=True),
                        "options": [
                            option.model_dump(by_alias=True, exclude_none=True)
                            for option in options
                        ],
                    },
                )
                if not isinstance(reply, dict):
                    return None
                outcome = reply
            elif controller is self._client:
                response = await asyncio.wait_for(
                    controller.request_permission(
                        session_id=session_id, tool_call=tool_call, options=options
                    ),
                    timeout=ACP_PERMISSION_TIMEOUT_SECONDS,
                )
                outcome = RequestPermissionResponse.model_validate(response).outcome.model_dump(
                    by_alias=True, exclude_none=True
                )
            else:
                return None
        except Exception:
            # An ACP controller exception is denial, never a raw error in Pi
            # RPC/model output or a reason to resend an uncertain MCP call.
            return None
        if self._active_turns.get(session_id) != turn_id:
            return None
        if isinstance(controller, SocketClient) and not self._runtime.is_controller(
            session_id, controller
        ):
            return None
        selected = outcome.get("optionId")
        if outcome.get("outcome") != "selected" or type(selected) is not str:
            return None
        if method == "confirm":
            return {"confirmed": selected == "allow-once"}
        if selected in choices:
            return {"value": choices[selected]}
        return None

    async def _run_agent_turn(
        self,
        session_id: str,
        thread_name: str,
        task: str,
        *,
        reply_targets: tuple[str, ...] = (),
        origins: tuple[Message, ...] = (),
        images: tuple[Any, ...] = (),
        original_keys: tuple[str, ...] = (),
        initial_display_text: str | None = None,
        autonomous_goal: bool = False,
        original_owner_input: bool = False,
        original_goal_id: str | None = None,
        dependency_wait_id: str | None = None,
    ) -> None:
        """Stream a real coding agent's reply: events to the client, status to the wire."""
        original_display = None if autonomous_goal else (initial_display_text or task)
        owner_task = asyncio.current_task()
        assert owner_task is not None
        thread = self._comms.registry.require(thread_name)
        thread_name = thread.name
        if backend.rpc_args_for(self._agent_bin, self._agent_args) is None and any(
            origin.seq > 0 for origin in origins
        ):
            # A text backend has no native user-start receipt. Its process
            # must not run for a bus input whose UNKNOWN row needs that proof.
            return
        goal = thread.goal
        wait = self._comms.goal_wait(thread_name)
        if wait is not None and not original_owner_input and wait.wait_id != dependency_wait_id:
            return
        if dependency_wait_id is not None and (wait is None or wait.wait_id != dependency_wait_id):
            return
        if original_owner_input and original_goal_id != (
            goal.id if goal is not None and goal.active else None
        ):
            raise RequestError.invalid_params({"reason": "input_authority_changed"})
        goal_permit: LaunchPermit | None = None
        if autonomous_goal and (goal is None or not goal.active):
            return
        if goal is not None and goal.active:
            if backend.rpc_args_for(self._agent_bin, self._agent_args) is None:
                if autonomous_goal:
                    return
                raise RequestError.invalid_params({"reason": "goal_requires_native_pi"})
            store = self._goal_store
            if (
                store is None
                and (self._comms.root / "goal-private" / "goal_attempts.sqlite3").exists()
            ):
                store = self._open_goal_store()
            if store is None:
                if autonomous_goal:
                    return
                raise RequestError.invalid_params({"reason": "goal_attempt_unavailable"})
            admission = self._comms.registry.snapshot().admission_generations[thread_name]
            with _store_lock(self._comms._wire_lock_path):
                generation = store.snapshot(goal.id)
                if generation is None or generation.state != "ready":
                    if autonomous_goal:
                        return
                    raise RequestError.invalid_params({"reason": "goal_attempt_unavailable"})
                try:
                    grant = self._ready_goal_grant_locked(thread, admission, store, generation)
                    reservation = store.reserve(goal.id, generation.number, ready_grant=grant)
                    goal_permit = store.claim_launch(reservation)
                except GoalAttemptError as error:
                    if autonomous_goal:
                        return
                    raise RequestError.invalid_params(
                        {"reason": "goal_attempt_unavailable"}
                    ) from error
        self._sessions[session_id] = thread_name
        # Error-display deduplication belongs to one backend turn, not a session.
        self._emitted_errors.pop(session_id, None)
        turn_id = uuid4().hex
        routing = TurnRouting(
            origins, MessageRoute(thread_name, reply_targets) if reply_targets else None
        )
        checkpoint = self._comms.transcript_checkpoint(thread_name)
        self._comms.begin_turn(thread_name, turn_id, task[:80], routing)
        turn_admission = self._comms.registry.snapshot().admission_generations[thread_name]
        direct_origins = tuple(
            origin
            for origin in origins
            if origin.seq > 0 and origin.target in self._comms.registry.aliases_for(thread_name)
        )
        bus_origins = tuple(origin for origin in origins if origin.seq > 0)
        if bus_origins:
            with _store_lock(self._comms._wire_lock_path):
                snapshot = self._comms.registry.snapshot()
                for origin in bus_origins:
                    key = self._dispositions.bus_key(origin, thread)
                    if self._dispositions.get(key) is None:
                        self._dispositions.record(
                            key,
                            seq=origin.seq,
                            owner=thread_name,
                            admission=snapshot.admission_generations[thread_name],
                            target=origin.target,
                            text=ScheduledTurn.incoming(origin, aliases=snapshot.aliases).prompt,
                        )
                    original_keys = (*original_keys, key)
        self._turn_input_keys[session_id] = set(original_keys)
        self._steering_input_keys[session_id] = {}
        self._steering_origins[session_id] = {}
        self._steering_goal_ids[session_id] = {}

        channel_batch = (
            len(origins) > 1
            and len({origin.seq for origin in origins}) == len(origins)
            and all(origin.seq > 0 and is_channel_target(origin.target) for origin in origins)
            and original_keys
            == tuple(self._dispositions.bus_key(origin, thread) for origin in origins)
            # The durable admission owns the exact prompt, including the
            # names resolved at admission. Re-deriving it here can drift if
            # a recipient was renamed before or after inbox draining.
            and (admitted_texts := self._dispositions.source_texts(original_keys)) is not None
            and task == "\n\n".join(admitted_texts)
        )

        def input_keys_valid(public_id: str | None, keys: tuple[str, ...], text: str) -> bool:
            # One authoritative user start proves every sequence in an exact
            # channel batch. Never credit an omitted/reordered original input,
            # a mixed direct batch, or a differently transformed native prompt.
            return len(keys) <= 1 or (
                public_id is None and channel_batch and keys == original_keys and text == task
            )

        @contextmanager
        def send_boundary(
            public_id: str | None, native_id: str, sent_text: str, *, already_bound: bool = False
        ) -> Iterator[bool | None]:
            # This lock spans the final authority read and stdin.write only.
            # Pi's turn, ACK, and provider response happen after it is released.
            with _store_lock(self._comms._wire_lock_path):
                snapshot = self._comms.registry.snapshot()
                canonical = snapshot.aliases.get(thread_name, thread_name)
                current = snapshot.threads.get(canonical)
                current_goal = current.goal if current is not None else None
                current_wait = self._comms.goal_wait(canonical) if current is not None else None
                if goal is not None and goal.active:
                    goal_ok = (
                        current_goal is not None
                        and current_goal.id == goal.id
                        and current_goal.active
                    )
                else:
                    goal_ok = current_goal is None or not current_goal.active
                input_permit = goal_permit
                admitted_goals = self._steering_goal_ids.get(session_id, {})
                owner_followup = public_id is not None and public_id in admitted_goals
                if owner_followup:
                    assert public_id is not None
                    admitted_goal_id = admitted_goals[public_id]
                    current_goal_id = (
                        current_goal.id
                        if current_goal is not None and current_goal.active
                        else None
                    )
                    goal_ok = admitted_goal_id == current_goal_id
                    input_permit = (
                        goal_permit
                        if goal_permit is not None
                        and goal_permit.reservation.goal_id == admitted_goal_id
                        else originated_attempts.get(admitted_goal_id or "")
                    )
                    if current_goal_id is not None and input_permit is None:
                        goal_ok = False
                keys = (
                    original_keys
                    if public_id is None
                    else (
                        (key,)
                        if (key := self._steering_input_keys.get(session_id, {}).get(public_id))
                        else ()
                    )
                )
                owner_ok = (
                    current is not None
                    and input_keys_valid(public_id, keys, sent_text)
                    and snapshot.statuses[canonical].running
                    and snapshot.admission_generations.get(canonical) == turn_admission
                    and current.pid == thread.pid
                    and current.created_at == thread.created_at
                    and current.worktree == thread.worktree
                    and current.active_turn is not None
                    and current.active_turn.id == turn_id
                )
                # A newly activated goal may supersede a follow-up that has
                # not yet been sent. Owner revocation still ends the turn.
                defer_for_goal = (
                    public_id is not None
                    and owner_ok
                    and (goal is None or not goal.active)
                    and current_goal is not None
                    and current_goal.active
                )
                allowed = (
                    owner_ok
                    and goal_ok
                    and (
                        current_wait is None
                        or owner_followup
                        or (public_id is None and original_owner_input)
                        or (
                            public_id is None
                            and current_wait.wait_id == dependency_wait_id
                            and any(
                                current_wait.matches(origin, snapshot) for origin in direct_origins
                            )
                        )
                    )
                    and not (
                        dependency_wait_id is not None
                        and public_id is None
                        and (current_wait is None or current_wait.wait_id != dependency_wait_id)
                    )
                    and not (
                        keys
                        and current_goal is not None
                        and current_goal.active
                        and not owner_followup
                        and not (
                            public_id is None
                            and (original_owner_input or dependency_wait_id is not None)
                        )
                    )
                )
                if allowed and input_permit is not None:
                    attempt = input_permit.reservation
                    assert self._goal_store is not None
                    allowed = self._goal_store._is_attempt(
                        attempt,
                        "claimed",
                        Generation(
                            attempt.goal_id,
                            attempt.generation,
                            "reserved",
                            attempt.attempt_id,
                        ),
                    )
                if allowed:
                    for key in keys:
                        row = self._dispositions.get(key)
                        if (
                            row is None
                            or row["status"] != "unknown"
                            or row["admission"] != snapshot.admission_generations[canonical]
                            or not (
                                row["native_id"] == native_id
                                and row["turn_id"] == turn_id
                                and row["sent_text"] == sent_text
                                if already_bound
                                else self._dispositions.bind(
                                    key,
                                    admission=row["admission"],
                                    turn_id=turn_id,
                                    native_id=native_id,
                                    text=sent_text,
                                )
                            )
                        ):
                            allowed = False
                            break
                if allowed and current_wait is not None:
                    allowed = self._comms.consume_goal_wait(canonical, current_wait.wait_id)
                if allowed:
                    if public_id is None:
                        display = original_display
                        input_origins = origins
                    else:
                        row = self._dispositions.get(keys[0]) if keys else None
                        display = row["source_text"] if row is not None else sent_text
                        origin = self._steering_origins.get(session_id, {}).get(public_id)
                        input_origins = (origin,) if origin is not None else ()
                    self._comms.record_input_display(
                        native_id,
                        display,
                        sent_text=sent_text,
                        routing=TurnRouting(input_origins, None) if input_origins else None,
                    )
                yield True if allowed else None if defer_for_goal else False

        def native_start(public_id: str | None, native_id: str, sent_text: str) -> bool:
            keys = (
                original_keys
                if public_id is None
                else (
                    (key,)
                    if (key := self._steering_input_keys.get(session_id, {}).get(public_id))
                    else ()
                )
            )
            return input_keys_valid(public_id, keys, sent_text) and all(
                self._dispositions.started(
                    key, turn_id=turn_id, native_id=native_id, text=sent_text
                )
                for key in keys
            )

        worktree = thread.worktree if Path(thread.worktree).is_dir() else str(Path.cwd())
        env_extra = {
            "AGENT_COMMS_THREAD": thread_name,
            "PI_AGENT_ID": thread_name,
            "AGENT_COMMS_ROOT": str(self._comms.root),
            "PI_PARENT_ID": thread.parent or "",
            "AGENT_COMMS_MANAGED": "1",
            "PI_WORKTREE": worktree,
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
            "Reply with comms_send when a reply is useful; otherwise call comms_dismiss "
            "with the channel to end quietly. Do not echo acknowledgments. "
            f"Your project directory is {thread.worktree!r}. Use comms_set_project(path) "
            "to change it persistently without creating another thread. After changing "
            "projects, end this turn; the runtime automatically resumes in the new project. "
            "When the user asks you to set or start a persistent goal, call "
            "comms_set_goal(text) so this same thread continues it autonomously. "
            f"Peer state: {json.dumps(peers)}\n\n{task}"
        )
        if goal is not None and goal.active:
            task = (
                f"Persistent goal {goal.id}: {goal.text}\nProgress: {goal.progress}\n"
                "Work toward this goal while respecting follow-up instructions. "
                "Use comms_goal with this goal_id to record useful progress. Set status completed "
                "only after verifying success, blocked when you need user input, or active "
                "to continue useful work in another turn. When waiting for delegated work, "
                "set status standby with explicit wait_for thread names and explain what you need. "
                "The goal stays active without polling; a direct message from a named dependency "
                "or an explicit user follow-up starts the next goal turn. "
                "Do not return empty output "
                "or repeatedly announce waiting. Do not wait or poll; "
                "the owner schedules continuation.\n\n" + task
            )
        if thread.auto_title_pending:
            task = (
                "Give this new thread a concise topic title before doing the task: call "
                "comms_rename_self with a meaningful 2-5-word hyphenated name (at most 48 "
                "characters). Summarize the user's intent; do not copy their first message, "
                "greeting, or request phrasing. This is automatic naming for a new thread.\n\n"
                + task
            )
        if reply_targets:
            task = (
                f"Your final answer is delivered automatically to {', '.join(reply_targets)}. "
                "Do not use comms_send to duplicate that answer.\n\n" + task
            )
        reply_parts: list[str] = []
        terminal_ok: bool | None = None
        successful_tool_observed = False
        goal_tool_ok = False
        goal_attempt_resolved = False
        originated_goal_ids: set[str] = set()
        originated_attempts: dict[str, LaunchPermit] = {}
        unattributed_usage: list[tuple[str, dict[str, Any]]] = []
        settled = False
        cancelled = False
        compaction_resume_activity: tuple[ActivityState, str] | None = None

        def update_turn_activity(state: ActivityState, detail: str) -> None:
            nonlocal compaction_resume_activity
            if compaction_resume_activity is not None:
                # Tool notifications may arrive while a summary is in flight.
                # Retain the next activity without hiding active compaction.
                compaction_resume_activity = (state, detail)
            else:
                self._comms.set_activity(thread_name, state, detail)

        backend_inbox: asyncio.Queue[str | dict[str, Any]] = asyncio.Queue()
        finish_event = asyncio.Event()
        controller = self._runtime.controller.get()
        if controller is UNBOUND_CONTROLLER:
            controller = None  # Autonomous/channel/goal turns have no controller.
        self._backend_inboxes[session_id] = backend_inbox
        self._active_turns[session_id] = turn_id
        try:
            await self._emit_event(session_id, self._started_event(thread_name, turn_id))
            await self._drain_inbox(session_id)
            session_file = thread.session_file
            fork_session = False
            if not session_file and thread.parent:
                session_file = self._comms.registry.require(thread.parent).session_file
                fork_session = bool(session_file)
            image_options: dict[str, Any] = {"images": images} if images else {}
            async for event in backend.stream_agent_events(
                self._agent_bin,
                (
                    backend.args_for_thinking_level(
                        backend.args_for_model(self._agent_args, thread.model),
                        thread.thinking_level,
                    )
                    if backend.rpc_args_for(self._agent_bin, self._agent_args) is not None
                    else self._agent_args
                ),
                task,
                worktree,
                env_extra,
                **image_options,
                session_file=session_file,
                fork_session=fork_session,
                steering_queue=backend_inbox,
                finish_event=finish_event,
                send_boundary=send_boundary,
                interrupt_boundary=lambda public_id, native_id, text: send_boundary(
                    public_id, native_id, text, already_bound=True
                ),
                native_start=native_start,
                persistent_session=(
                    self._persistent_backends.setdefault(session_id, backend.PersistentPiSession())
                    if backend.rpc_args_for(self._agent_bin, self._agent_args) is not None
                    else None
                ),
                ui_request=lambda request: self._extension_ui_permission(
                    session_id, turn_id, controller, request
                ),
            ):
                kind = event.get("type")
                if kind == "steering_interrupted":
                    reply_parts.clear()
                if kind == "input_refused":
                    input_id = event.get("id")
                    if isinstance(input_id, str):
                        self._forwarded_inputs.get(session_id, set()).discard(input_id)
                        refused_key = self._steering_input_keys.get(session_id, {}).get(input_id)
                        if refused_key is not None:
                            # This input was denied before stdin.write. Keep
                            # its persisted UNKNOWN row visible, but do not
                            # count it as an unstarted sent follow-up.
                            self._turn_input_keys.get(session_id, set()).discard(refused_key)
                        if self._queued_inputs.get(session_id, {}).pop(input_id, None):
                            await self._emit_queue_state(session_id)
                if kind == "provider_usage":
                    response_id = str(event["response_id"])
                    usage = event["usage"]
                    current_goal = self._comms.registry.require(thread_name).goal
                    current_goal_id = current_goal.id if current_goal is not None else None
                    permit = originated_attempts.get(current_goal_id or "")
                    if (
                        permit is None
                        and goal_permit is not None
                        and (current_goal_id == goal_permit.reservation.goal_id)
                    ):
                        permit = goal_permit
                    if permit is not None:
                        assert self._goal_store is not None
                        self._goal_store.record_provider_usage(permit, response_id, usage)
                    else:
                        unattributed_usage.append((response_id, usage))
                    continue
                if kind in {"compaction_start", "compaction_end"}:
                    if kind == "compaction_start":
                        if compaction_resume_activity is None:
                            current_activity = self._comms.activity_of(thread_name)
                            compaction_resume_activity = (
                                current_activity.state,
                                current_activity.detail,
                            )
                        self._comms.set_activity(
                            thread_name, ActivityState.WORKING, "Compacting context"
                        )
                    elif compaction_resume_activity is not None:
                        self._comms.set_activity(thread_name, *compaction_resume_activity)
                        compaction_resume_activity = None
                    # A previous usage sample is not authoritative after Pi
                    # starts compaction, even if the attempt later aborts.
                    info = self._comms.agent_info_of(thread_name)
                    self._comms.set_agent_info(
                        thread_name,
                        model=info.model if info else None,
                        session_name=info.session_name if info else None,
                        context_used=None,
                        context_size=info.context_size if info else None,
                    )
                if reply_targets and kind == "chunk":
                    reply_parts.append(str(event.get("text") or ""))
                if kind == "tool_end" and event.get("ok") is True:
                    successful_tool_observed = True
                if kind == "done":
                    unknown_attempts = any(
                        self._dispositions.status(key) != "started"
                        for key in self._turn_input_keys.get(session_id, set())
                    )
                    if event.get("ok") is True and (
                        self._forwarded_inputs.get(session_id) or unknown_attempts
                    ):
                        # A final assistant stop can prove the original turn,
                        # not an ACKed follow-up lacking its own user start.
                        event = {
                            **event,
                            "ok": False,
                            "text": (
                                "An identified follow-up input was not started; "
                                "inspect local diagnostics."
                            ),
                        }
                    if terminal_ok is not None:
                        terminal_ok = False
                    elif event.get("ok") is True:
                        terminal_ok = True
                    else:
                        terminal_ok = False
                if kind == "input_started":
                    input_id = event.get("id")
                    started_keys = (
                        original_keys
                        if input_id is None
                        else (
                            (steering_key,)
                            if isinstance(input_id, str)
                            and (
                                steering_key := self._steering_input_keys.get(session_id, {}).get(
                                    input_id
                                )
                            )
                            else ()
                        )
                    )
                    for key in started_keys:
                        row = self._dispositions.get(key)
                        if row is not None and row["status"] == "started":
                            await self._emit_input_disposition(session_id, row)
                    if isinstance(input_id, str):
                        self._forwarded_inputs.get(session_id, set()).discard(input_id)
                    item = self._queued_inputs.get(session_id, {}).pop(input_id or "", None)
                    await self._emit_input_started(
                        session_id,
                        (
                            item.text
                            if item and item.echo
                            else initial_display_text if input_id is None else None
                        ),
                        input_id,
                    )
                    await self._emit_queue_state(session_id)
                if (
                    kind == "done"
                    and goal is not None
                    and goal.active
                    and self._comms.registry.require(thread_name).worktree == thread.worktree
                ):
                    current_goal = self._comms.registry.require(thread_name).goal
                    failed = not event.get("ok")
                    # An RPC stream can settle and exit successfully after
                    # accepting a user prompt without assistant output or tool
                    # activity. This is not a productive goal turn.
                    empty_success = (
                        not failed
                        and not successful_tool_observed
                        and not str(event.get("text") or "").strip()
                    )
                    if (
                        current_goal
                        and current_goal.id == goal.id
                        and current_goal.active
                        and (failed or empty_success)
                    ):
                        # Block the latest same-ID goal under the wire lock,
                        # retaining any newer progress from a concurrent update.
                        self._comms.block_goal_after_failed_turn(
                            thread_name,
                            started_goal=goal,
                            expected_worktree=thread.worktree,
                            diagnostic=(
                                "Backend turn failed; inspect local diagnostics before resuming."
                                if failed
                                else "Backend reported success without assistant output "
                                "or tool activity; inspect the session before resuming."
                            ),
                        )
                if kind == "model_changed":
                    future = self._model_requests.get(event.get("id", ""))
                    if future is not None and not future.done():
                        if event.get("ok"):
                            future.set_result(None)
                        else:
                            future.set_exception(RuntimeError(str(event.get("error"))))
                elif kind == "thinking_changed":
                    future = self._thinking_requests.get(event.get("id", ""))
                    if future is not None and not future.done():
                        if event.get("ok"):
                            future.set_result(None)
                        else:
                            future.set_exception(RuntimeError(str(event.get("error"))))
                elif kind == "agent_info":
                    session_name = event.get("session_name")
                    session_file = event.get("session_file")
                    if (
                        session_file
                        and self._comms.registry.require(thread_name).session_file != session_file
                    ):
                        self._comms.attach_session(thread_name, str(session_file))
                    self._comms.set_agent_info(
                        thread_name,
                        model=event.get("model"),
                        session_name=session_name,
                        context_used=event.get("context_used"),
                        context_size=event.get("context_size"),
                    )
                    if (
                        event.get("model")
                        and self._comms.registry.require(thread_name).model is None
                    ):
                        self._comms.set_thread_model(thread_name, event["model"])
                        await self._runtime.session_update(
                            session_id=session_id,
                            update=ConfigOptionUpdate(
                                session_update="config_option_update",
                                config_options=await self._config_options(thread_name),
                            ),
                        )
                    if (
                        event.get("thinking_level")
                        and self._comms.registry.require(thread_name).thinking_level is None
                    ):
                        self._comms.set_thread_thinking_level(thread_name, event["thinking_level"])
                elif kind == "tool_start":
                    update_turn_activity(ActivityState.WORKING, event.get("title", ""))
                elif kind == "tool_end":
                    if event.get("name") == "comms_goal" and event.get("ok") is True:
                        goal_tool_ok = True
                    thread_name = await self._sync_session_identity(session_id)
                    if event.get("name") == "comms_set_goal" and event.get("ok") is True:
                        current_goal = self._comms.registry.require(thread_name).goal
                        if current_goal is not None:
                            store = self._open_goal_store()
                            if store.snapshot(current_goal.id) is None:
                                store.create_goal(current_goal.id)
                                originated_goal_ids.add(current_goal.id)
                                grant = store.ready_grant(current_goal.id, 1)
                                reservation = store.reserve(current_goal.id, 1, ready_grant=grant)
                                origin_permit = store.claim_launch(reservation)
                                originated_attempts[current_goal.id] = origin_permit
                                for response_id, usage in unattributed_usage:
                                    store.record_provider_usage(origin_permit, response_id, usage)
                                unattributed_usage.clear()
                                self._pending_goal_origins[thread_name] = current_goal.id
                    update_turn_activity(ActivityState.THINKING, task[:80])
                elif kind == "settled":
                    compaction_resume_activity = None
                    self._comms.finish_turn(thread_name, turn_id)
                    settled = True
                    finish_event.set()
                    self._active_turns.pop(session_id, None)
                await self._emit_event(
                    session_id, {**event, "turn_id": turn_id, "route": routing.reply}
                )
                if kind == "tool_end":
                    await self._sync_goal_execution(session_id, thread_name)
                    sent = await asyncio.to_thread(
                        self._comms.sent_tool_message,
                        event.get("name", ""),
                        event.get("output", ""),
                        bool(event.get("ok")),
                    )
                    if sent is not None:
                        await self._emit_event(
                            session_id,
                            {
                                "type": "sent",
                                "text": sent.body,
                                "route": MessageRoute(sent.sender, (sent.target,)),
                            },
                        )
                if kind in {"input_started", "done", "settled"}:
                    await self._sync_goal_execution(session_id, thread_name)
            if terminal_ok is None and goal is not None and goal.active:
                # An EOF without a done event is a failed turn, not a signal to
                # schedule the still-active goal again on the next live drain.
                current_thread = self._comms.registry.require(thread_name)
                current_goal = current_thread.goal
                if (
                    current_thread.worktree == thread.worktree
                    and current_goal is not None
                    and current_goal.id == goal.id
                    and current_goal.active
                ):
                    self._comms.block_goal_after_failed_turn(
                        thread_name,
                        started_goal=goal,
                        expected_worktree=thread.worktree,
                        diagnostic=(
                            "Backend turn ended without a result; " "inspect local diagnostics."
                        ),
                    )
            if goal_permit is not None:
                assert goal is not None
                assert self._goal_store is not None
                current_goal = self._comms.registry.require(thread_name).goal
                verified_report = (
                    goal_tool_ok
                    and current_goal is not None
                    and current_goal.id == goal.id
                    and current_goal.reported_turn == turn_id
                )
                if terminal_ok is True and current_goal is not None and current_goal.id == goal.id:
                    witness = f"native-terminal:{turn_id}"
                    if current_goal.status == "completed" and verified_report:
                        witness = f"registry-revision:{current_goal.revision}"
                        self._goal_store.record_verified_completion(goal_permit, witness)
                        goal_attempt_resolved = True
                    elif current_goal.active or current_goal.status == "paused":
                        # A successful in-flight turn may finish after owner pause.
                        # Preserve success; the scheduler will not launch while paused.
                        self._goal_store.record_verified_progress(goal_permit, witness)
                        goal_attempt_resolved = True
                if not goal_attempt_resolved:
                    with suppress(StaleAttempt):
                        self._goal_store.record_failed(
                            goal_permit.reservation,
                            "Goal turn ended without verified terminal progress.",
                        )
                    goal_attempt_resolved = True
                    if current_goal is not None and current_goal.id == goal.id:
                        diagnostic = "Goal turn ended without verified terminal progress."
                        self._comms.block_goal_after_failed_turn(
                            thread_name,
                            started_goal=goal,
                            expected_worktree=thread.worktree,
                            diagnostic=diagnostic,
                        )
            if origins and settled and terminal_ok is True:
                await asyncio.to_thread(
                    self._comms.record_turn_routing, thread_name, checkpoint, routing
                )
            if terminal_ok is True:
                if reply_parts:
                    for target in reply_targets:
                        self._comms.send(thread_name, target, "".join(reply_parts))
            else:
                # Streamed chunks were provisional. A failed or missing terminal
                # result cannot turn them into a completed wire reply. Keep the
                # failure notice non-waking, including for human reply targets.
                # Backend text may include stderr, secrets, or content from an
                # unrelated session. Only the local client gets that diagnostic.
                notice_targets = tuple(
                    dict.fromkeys(
                        (*reply_targets,)
                        + tuple(
                            origin.target if is_channel_target(origin.target) else origin.sender
                            for origin in origins
                            if origin.sender != thread_name
                        )
                    )
                )
                for target in notice_targets:
                    prefix = "Request failed" if target in reply_targets else "Delivery failed"
                    self._comms.send(
                        thread_name,
                        target,
                        f"{prefix}: backend turn did not complete; inspect local diagnostics.",
                        MessageType.ALERT,
                        notice=True,
                    )
            await self._runtime.session_update(
                session_id=session_id,
                update=AgentMessageChunk(
                    session_update="agent_message_chunk",
                    content=TextContentBlock(type="text", text=""),
                    field_meta={
                        "agentComms": {
                            "transcriptChanged": True,
                            "transcriptCursor": asdict(
                                self._comms.transcript_checkpoint(thread_name)
                            ),
                        }
                    },
                ),
            )
        except asyncio.CancelledError:
            cancelled = True
            await backend.terminate_task_process(owner_task)
            raise
        except Exception:
            await backend.terminate_task_process(owner_task)
            raise
        finally:
            for originated_id in originated_goal_ids:
                current = self._comms.registry.require(thread_name).goal
                valid_origin = (
                    terminal_ok is True
                    and current is not None
                    and current.id == originated_id
                    and current.status in {"active", "paused", "completed"}
                )
                assert self._goal_store is not None
                resolved_origin_permit = originated_attempts.get(originated_id)
                if resolved_origin_permit is not None:
                    if (
                        valid_origin
                        and current is not None
                        and current.status in {"active", "paused"}
                    ):
                        self._goal_store.record_verified_progress(
                            resolved_origin_permit, f"origin-final:{turn_id}"
                        )
                    elif (
                        valid_origin
                        and current is not None
                        and current.status == "completed"
                        and goal_tool_ok
                        and current.reported_turn == turn_id
                    ):
                        self._goal_store.record_verified_completion(
                            resolved_origin_permit, f"origin-completed:{current.revision}"
                        )
                    else:
                        with suppress(StaleAttempt):
                            self._goal_store.record_failed(
                                resolved_origin_permit.reservation,
                                "Goal origin turn did not finish successfully.",
                            )
                        valid_origin = False
                elif not valid_origin:
                    generation = self._goal_store.snapshot(originated_id)
                    if generation is not None and generation.state in {"ready", "reserved"}:
                        self._goal_store.retire_goal(
                            originated_id,
                            expected_generation=generation.number,
                            attempt_id=generation.attempt_id,
                        )
                if (
                    not valid_origin
                    and current is not None
                    and current.id == originated_id
                    and current.status in {"active", "completed"}
                ):
                    self._comms.update_goal(
                        thread_name,
                        "blocked",
                        goal_id=originated_id,
                        expected_goal=current,
                        progress="Goal origin turn did not finish successfully.",
                    )
                if self._pending_goal_origins.get(thread_name) == originated_id:
                    self._pending_goal_origins.pop(thread_name, None)
            if goal_permit is not None and not goal_attempt_resolved:
                assert self._goal_store is not None
                with suppress(StaleAttempt):
                    self._goal_store.record_failed(
                        goal_permit.reservation,
                        "Goal attempt ended without a verified terminal result.",
                    )
            self._emitted_errors.pop(session_id, None)
            if self._backend_inboxes.get(session_id) is backend_inbox:
                self._backend_inboxes.pop(session_id, None)
            while not backend_inbox.empty():
                pending = backend_inbox.get_nowait()
                if isinstance(pending, str):
                    self._pending_turns.setdefault(session_id, []).append(ScheduledTurn(pending))
            thread_name = await self._sync_session_identity(session_id)
            current_project = self._comms.registry.require(thread_name).worktree
            if current_project != thread.worktree and (
                persistent := self._persistent_backends.get(session_id)
            ):
                await persistent.close_idle()
            # Discard unresolved per-turn IDs without replay. Durable ACP input
            # disposition across owner crashes remains a separate integration.
            self._forwarded_inputs.pop(session_id, None)
            self._steering_input_keys.pop(session_id, None)
            self._steering_origins.pop(session_id, None)
            self._steering_goal_ids.pop(session_id, None)
            self._turn_input_keys.pop(session_id, None)
            remaining = self._queued_inputs.pop(session_id, {})
            if remaining:
                await self._emit_queue_state(
                    session_id,
                    restored=[item.text for item in remaining.values() if item.echo],
                )
            if not cancelled and not self._closing and current_project != thread.worktree:
                self._pending_turns.setdefault(session_id, []).append(
                    ScheduledTurn(
                        f"Project change completed: tools and context now use {current_project!r}. "
                        "Continue the user's previous request from this directory. "
                        "If the request was only to switch projects, report that you are ready; "
                        "do not invent extra work."
                    )
                )
            if not settled:
                self._comms.finish_turn(thread_name, turn_id)
                self._active_turns.pop(session_id, None)
                await self._emit_event(session_id, {"type": "settled", "turn_id": turn_id})

    def _started_event(self, thread_name: str, turn_id: str) -> dict[str, Any]:
        """Project one owner-authored turn without inventing presentation timestamps."""
        thread = self._comms.registry.require(thread_name)
        active = thread.active_turn
        activity = self._comms.activity_of(thread.name)
        return {
            "type": "started",
            "turn_id": turn_id,
            **(
                {"started_at": active.started_at}
                if active is not None and active.id == turn_id
                else {}
            ),
            "activity": activity.state.value,
            "activity_detail": activity.detail,
        }

    async def replay_turn_state(self, session_id: str, client: Any = None) -> None:
        thread_name = self._require_session(session_id)
        active = self._comms.registry.require(thread_name).active_turn
        event = (
            self._started_event(thread_name, active.id)
            if active is not None
            else {"type": "settled", "turn_id": ""}
        )
        await self._emit_event(session_id, event, client=client)

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
        for persistent in self._persistent_backends.values():
            await persistent.close_idle()
        self._persistent_backends.clear()
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
                    and self._comms.registry.status(canonical).running
                ):
                    self._comms.stop(canonical)
            except Exception as error:
                self._debug_log(f"shutdown error: {error!r}")
        await self._runtime.close()

    async def _emit_text(
        self, session_id: str, text: str, client: Any = None, route: MessageRoute | None = None
    ) -> None:
        if not text:
            return
        await (client or self._runtime).session_update(
            session_id=session_id,
            update=AgentMessageChunk(
                session_update="agent_message_chunk",
                content=TextContentBlock(type="text", text=text),
                field_meta={"agentComms": {"route": asdict(route) if route else None}},
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
        elif kind in {"chunk", "assistant", "notice", "sent"}:
            text = event.get("text") or ""
            if text:
                await self._emit_text(session_id, text, client, event.get("route"))
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
                content=[
                    ContentToolCallContent.model_validate(item)
                    for item in tool_result_content(
                        event["id"], event.get("output") or "", event.get("diff")
                    )
                ],
            )
            await client.session_update(session_id=session_id, update=end_update)
        elif kind == "thinking":
            await client.session_update(
                session_id=session_id,
                update=AgentThoughtChunk(
                    session_update="agent_thought_chunk",
                    content=TextContentBlock(type="text", text=event.get("text") or ""),
                ),
            )
        elif kind == "mcp_live_status":
            await client.session_update(
                session_id=session_id,
                update=AgentMessageChunk(
                    session_update="agent_message_chunk",
                    content=TextContentBlock(type="text", text=""),
                    field_meta={"agentComms": {"mcpClient": event["receipt"]}},
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
        elif kind == "compaction_progress":
            chunk_index = event.get("chunk_index")
            done = event.get("source_bytes_done")
            total = event.get("source_bytes_total")
            measured = type(done) is int and type(total) is int and 0 <= done <= total and total > 0
            if type(chunk_index) is int and (chunk_index > 0 or chunk_index == 0 and measured):
                await client.session_update(
                    session_id=session_id,
                    update=AgentMessageChunk(
                        session_update="agent_message_chunk",
                        content=TextContentBlock(type="text", text=""),
                        field_meta={
                            "agentComms": {
                                "compaction": {
                                    "phase": "progress",
                                    "status": "running",
                                    "chunkIndex": chunk_index,
                                    **(
                                        {"sourceBytesDone": done, "sourceBytesTotal": total}
                                        if measured
                                        else {}
                                    ),
                                    **(
                                        {"summaryPhase": event["summary_phase"]}
                                        if isinstance(event.get("summary_phase"), str)
                                        and event["summary_phase"]
                                        else {}
                                    ),
                                }
                            }
                        },
                    ),
                )
        elif kind in {"compaction_start", "compaction_end"}:
            phase = (
                "start"
                if kind == "compaction_start"
                else "end" if event.get("aborted") is False else "abort"
            )
            reason = event.get("reason")
            if reason not in {"manual", "threshold", "overflow", "unknown"}:
                reason = "unknown"
            summary = ""
            if phase == "end":
                summary = self._sanitized_compaction_summary(event.get("summary"))
            status = {"start": "running", "end": "completed", "abort": "aborted"}[phase]
            status_text = {
                "start": "",
                "end": "Context compacted; usage is recalculating.",
                "abort": "Context compaction aborted; usage is unknown.",
            }[phase]
            if summary:
                status_text += f" Summary: {summary}"
            detail: dict[str, Any] = {
                "phase": phase,
                "status": status,
                "reason": reason,
                "contextUsed": None,
                "contextState": "unknown",
                "willRetry": event.get("will_retry") is True,
            }
            if summary:
                detail["summary"] = summary
            await client.session_update(
                session_id=session_id,
                update=AgentMessageChunk(
                    session_update="agent_message_chunk",
                    content=TextContentBlock(type="text", text=status_text),
                    field_meta={"agentComms": {"compaction": detail}},
                ),
            )
        elif kind in {"started", "settled"}:
            lifecycle = {
                "turnStarted" if kind == "started" else "turnSettled": True,
                "turnId": event.get("turn_id"),
            }
            if kind == "started":
                lifecycle.update(
                    {
                        **(
                            {"startedAt": event["started_at"]}
                            if event.get("started_at") is not None
                            else {}
                        ),
                        **(
                            {"activity": event["activity"]}
                            if event.get("activity") is not None
                            else {}
                        ),
                        **(
                            {"activityDetail": event["activity_detail"]}
                            if event.get("activity_detail") is not None
                            else {}
                        ),
                    }
                )
            await client.session_update(
                session_id=session_id,
                update=AgentMessageChunk(
                    session_update="agent_message_chunk",
                    content=TextContentBlock(type="text", text=""),
                    field_meta={"agentComms": lifecycle},
                ),
            )
        elif kind == "error":
            text = str(event.get("text") or "Backend failed")
            self._emitted_errors[session_id] = text
            await self._emit_text(session_id, f"[agent error] {text}", client)
        elif kind == "done":
            prior_error = self._emitted_errors.pop(session_id, None)
            if not event.get("ok") and event.get("text"):
                text = str(event["text"])
                # An explicit error event in this turn already showed the failure.
                if prior_error != text:
                    await self._emit_text(session_id, f"[agent error] {text}", client)

    @staticmethod
    def _sanitized_compaction_summary(value: Any) -> str:
        """Preserve the saved Markdown summary, excluding terminal control codes."""
        return backend.compaction_summary(value)

    @staticmethod
    def _prompt_text(prompt: list[Any]) -> str:
        chunks: list[str] = []
        for block in prompt:
            if isinstance(block, dict):
                chunks.append(str(block.get("text", "")))
            else:
                chunks.append(str(getattr(block, "text", "") or ""))
        return "\n".join(chunk for chunk in chunks if chunk)


class CommsClient(CommsAgent):
    """A stdio connection is an attachment, never a thread's executor."""

    async def new_session(
        self, cwd: str, mcp_servers: list[Any] | None = None, **kwargs: Any
    ) -> NewSessionResponse:
        self._reject_foreign_mcp(mcp_servers)
        thread = self._declare_thread(cwd, 0)
        loaded = await self.load_session(cwd, thread.name, mcp_servers, **kwargs)
        return NewSessionResponse(
            session_id=thread.name,
            config_options=loaded.config_options,
            field_meta=loaded.field_meta,
        )

    async def load_session(
        self, cwd: str, session_id: str, mcp_servers: list[Any] | None = None, **kwargs: Any
    ) -> LoadSessionResponse:
        self._reject_foreign_mcp(mcp_servers)
        thread = self._validated_thread(cwd, session_id)
        owner = await asyncio.to_thread(
            self._comms.ensure_owner,
            thread.name,
            agent_bin=self._agent_bin,
            agent_args=self._agent_args,
        )
        return await self._attach_owner(owner, session_id)


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
    if "--login" in sys.argv[1:]:
        from .login import run_login

        index = sys.argv.index("--login")
        return run_login(sys.argv[index + 1] if index + 1 < len(sys.argv) else "")
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
        agent = CommsClient(comms, runtime_enabled=True)

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
