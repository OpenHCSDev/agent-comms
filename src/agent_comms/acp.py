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
import hashlib
import json
import os
import re
import shlex
import sqlite3
import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from dataclasses import asdict, replace
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast
from uuid import uuid4

from acp import RequestError, run_agent
from acp.schema import (
    AgentMessageChunk,
    ContentToolCallContent,
    InitializeResponse,
    LoadSessionResponse,
    NewSessionResponse,
    PermissionOption,
    PromptResponse,
    RequestPermissionResponse,
    SessionInfoUpdate,
    SetSessionConfigOptionResponse,
    TextContentBlock,
    ToolCallUpdate,
)

from . import agent_events as events
from . import backend
from .agent_event_updates import AcpEventConsumer
from .bus_publication import stable_thread_lookup, unique_wire_object
from .cohort_foreground import _accept_visible_initials, _preflight
from .coordinated_runtime import run_one_sealed_claim
from .coordination import CoordinationError, WakeClaim
from .coordination_store import (
    IdentityConflict,
    MutationStore,
    PublicationActivationBlocked,
    StaleFence,
)
from .declarations import (
    ActivityState,
    FinishedTurnFence,
    Goal,
    GoalExecution,
    Message,
    MessageBus,
    MessageRoute,
    MessageType,
    RelationViolationError,
    ScheduledTurn,
    Thread,
    TurnLeaseFence,
    TurnRouting,
    _store_lock,
    is_channel_target,
)
from .diagnostics import record_terminal_failure, terminal_failure_reason
from .goal_actions import (
    BlockedGoalAction,
    EditGoalAction,
    GoalAction,
    GoalPrecondition,
    OwnerControlInvocable,
    OwnerInvocable,
    PausedGoalAction,
    RetryGoalAction,
    SetGoalAction,
)
from .goal_attempts import (
    Generation,
    GoalAttemptError,
    GoalAttemptStore,
    LaunchPermit,
    StaleAttempt,
    UnresolvedAttempt,
)
from .goal_failure_observation import FailedTurnObservation
from .goal_states import ActiveGoal, CompletedGoal, PausedGoal
from .input_drain import InputDrain, QueuedInput
from .input_effects import InputEffects
from .mro_dispatch import MroDispatch, handles
from .native_source_cursor import advance_current_native_cursor, read_current_native_cursor
from .operations import Comms, wire
from .pending_requests import PendingRequests
from .runtime import (
    ACP_PERMISSION_TIMEOUT_SECONDS,
    UNBOUND_CONTROLLER,
    RuntimeProxy,
    RuntimeServer,
    SocketClient,
    socket_path,
)
from .selected_summary_admission import SelectedAdmissionIdentity, SelectedSummaryAdmission
from .selected_write_plan import PlannedWrite, SelectedWritePlans
from .session_effects import SessionEffects
from .session_lifecycle import AttachedSessionLifecycle, SessionLifecycle
from .transcript_updates import SentTranscriptUpdate, StartedTranscriptUpdate, TranscriptUpdate

if TYPE_CHECKING:
    from .selected_tool_broker import SelectedToolIntent  # type: ignore[import-not-found]

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
NO_REPLY_WINDOW = 2.5  # silence: end the turn after this long with nothing
REPLY_WINDOW = 8.0  # once replies flow, keep collecting at most this long
REPLY_QUIET = 1.5  # after the last reply, wait this long then end the turn
REPLY_POLL = 0.25
ACTIVITY_WINDOW = 60.0  # keep the turn open while a peer is thinking/working
IDLE_GRACE = 1.0  # peers idle for this long -> drain and end the turn


def _goal_attempt_unavailable() -> RequestError:
    return RequestError.invalid_params(
        {
            "reason": "goal_attempt_unavailable",
            "details": "The goal has no launchable attempt. Inspect its state and use "
            "Retry for a failed attempt; no prompt was sent to Pi.",
        }
    )


class CommsAgent(SessionEffects, InputEffects):
    """ACP agent bound to one Comms wire.

    Session -> thread. Coding prompts run the configured backend; targeted
    prompts relay through the shared wire and drain replies back to the client.
    """

    session_lifecycle_class = SessionLifecycle

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
        adaptive_compaction_enabled: bool = True,
        adaptive_summary_strategy: Any = None,
        private_nk_native_package: Path | None = None,
        private_nk_wire_root_id: str | None = None,
        private_selected_tool_intent: SelectedToolIntent | None = None,
    ):
        if (private_nk_native_package is None) != (private_nk_wire_root_id is None):
            raise ValueError("private N/K ACP requires both reviewed Pi package and exact root")
        if private_selected_tool_intent is not None:
            from .selected_tool_broker import SelectedToolIntent

            if type(private_selected_tool_intent) is not SelectedToolIntent:
                raise TypeError("private selected tool needs a nominal owner intent")
            if private_nk_wire_root_id is None or private_nk_native_package is None:
                raise ValueError("private selected tool requires exact N/K root and native package")
        self._private_selected_tool_intent = private_selected_tool_intent
        self._private_nk_native_package = private_nk_native_package
        self._private_nk_wire_root_id = private_nk_wire_root_id
        self._private_cursor_announced: dict[str, str] = {}
        # Local ACP projection order, allocated before any async notification.
        # This is informational UI ordering, never a native input disposition.
        self._private_cursor_revisions: dict[str, int] = {}
        self._comms = comms
        # Enabled for verified native owners by default. Explicit construction
        # may disable it; model/tool content cannot change this owner policy.
        self._adaptive_compaction_enabled = adaptive_compaction_enabled
        self._adaptive_summary_strategy = adaptive_summary_strategy
        self._agent_bin = agent_bin or os.environ.get("AGENT_COMMS_AGENT_BIN", DEFAULT_AGENT_BIN)
        arg_env = os.environ.get("AGENT_COMMS_AGENT_ARGS")
        self._agent_args = (
            agent_args
            if agent_args is not None
            else (shlex.split(arg_env) if arg_env is not None else list(DEFAULT_AGENT_ARGS))
        )
        self._turn_tasks: dict[str, asyncio.Task[Any]] = {}
        self._persistent_backends: dict[str, backend.PersistentPiSession] = {}
        self._goal_store: GoalAttemptStore | None = None
        self._pending_goal_origins: dict[str, str] = {}
        self._runtime = RuntimeServer(self)
        self.sessions = self.session_lifecycle_class(
            comms, self._agent_bin, self._agent_args, self._runtime, runtime_enabled, self
        )
        self.config = self.sessions.config
        self.inputs = InputDrain(comms, self.sessions, self._runtime, self, auto_wake)
        # A preplanned write is valid only while its original ACP controller
        # remains attached. Never restore this binding after a process crash.
        self._selected_write_controllers: dict[tuple[str, int], tuple[str, object]] = {}
        self._turn_locks: dict[str, asyncio.Lock] = {}
        self._active_turns: dict[str, str] = {}
        self._emitted_errors: dict[str, str] = {}
        self._goal_execution_signatures: dict[str, tuple[Goal | None, GoalExecution | None]] = {}
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

    def _initialize_session_delivery(
        self, session_id: str, thread: Thread, *, fresh: bool, private: bool
    ) -> None:
        return self.inputs.initialize_session_delivery(
            session_id, thread, fresh=fresh, private=private
        )

    def _create_runtime_proxy(self, thread: Thread, session_id: str) -> RuntimeProxy:
        return RuntimeProxy(self, session_id, socket_path(self._comms.root, thread.pid))

    async def _close_idle_backend(self, session_id: str) -> None:
        if session_id not in self._active_turns and (
            persistent := self._persistent_backends.get(session_id)
        ):
            await persistent.close_idle()

    def _active_backend_inbox(self, session_id: str) -> asyncio.Queue[str | dict[str, Any]] | None:
        return (
            self.inputs.backend_inboxes.get(session_id)
            if session_id in self._active_turns
            else None
        )

    # Private attachment ABI consumed by runtime/compaction. State exists only
    # on its component; in-process consumers use that owner directly.
    @property
    def _runtime_enabled(self) -> bool:
        return self.sessions.runtime_enabled

    @property
    def _sessions(self) -> dict[str, str]:
        return self.sessions.bindings

    @property
    def _session_titles(self) -> dict[str, str]:
        return self.sessions.titles

    @property
    def _display_titles(self) -> dict[str, str | None]:
        return self.sessions.display_titles

    @property
    def _session_worktrees(self) -> dict[str, str]:
        return self.sessions.worktrees

    @property
    def _proxies(self) -> dict[str, RuntimeProxy]:
        return self.sessions.proxies

    @property
    def _proxy_image_support(self) -> dict[str, bool]:
        return self.sessions.proxy_image_support

    @property
    def _transcript_snapshots(self) -> bool:
        return self.sessions.transcript.snapshots

    @property
    def _transcript_diffs(self) -> bool:
        return self.sessions.transcript.diffs

    @property
    def _setting_requests(self) -> PendingRequests:
        return self.config.setting_requests

    @property
    def _client(self) -> Any:
        return self.sessions.client

    @_client.setter
    def _client(self, client: Any) -> None:
        self.sessions.client = client

    def on_connect(self, client: Any) -> None:
        """Called by AgentSideConnection with the client-facing connection."""
        self.sessions.client = client

    # ─── ACP methods ─────────────────────────────────────────────────────────

    @property
    def _drain_tasks(self):
        return self.inputs.drain_tasks

    @property
    def _backend_inboxes(self):
        return self.inputs.backend_inboxes

    @property
    def _queued_inputs(self):
        return self.inputs.queued_inputs

    @property
    def _restored_inputs(self):
        return self.inputs.restored_inputs

    @property
    def _queue_revisions(self):
        return self.inputs.queue_revisions

    @property
    def _forwarded_inputs(self):
        return self.inputs.forwarded_inputs

    @property
    def _steering_input_keys(self):
        return self.inputs.steering_input_keys

    @property
    def _steering_origins(self):
        return self.inputs.steering_origins

    @property
    def _steering_goal_ids(self):
        return self.inputs.steering_goal_ids

    @property
    def _turn_input_keys(self):
        return self.inputs.turn_input_keys

    @property
    def _turn_original_input_keys(self):
        return self.inputs.turn_original_input_keys

    @property
    def _selected_summary_admissions(self):
        return self.inputs.selected_summary_admissions

    @property
    def _turn_input_text(self):
        return self.inputs.turn_input_text

    @property
    def _dispositions(self):
        return self.inputs.dispositions

    @property
    def _delivery_cursors(self):
        return self.inputs.delivery_cursors

    @property
    def _passive_awareness(self):
        return self.inputs.passive_awareness

    @property
    def _legacy_through(self):
        return self.inputs.legacy_through

    @property
    def _pending_turns(self):
        return self.inputs.pending_turns

    @property
    def _direct_interrupt_tickets(self):
        return self.inputs.direct_interrupt_tickets

    @property
    def _drain_locks(self):
        return self.inputs.drain_locks

    @property
    def _inbox_cursors(self):
        return self.inputs.inbox_cursors

    @property
    def _wake_tasks(self):
        return self.inputs.wake_tasks

    @property
    def _closing(self):
        return self.inputs.closing

    @property
    def _auto_wake(self):
        return self.inputs.auto_wake

    async def initialize(
        self,
        protocol_version: int,
        client_capabilities: Any = None,
        client_info: Any = None,
    ) -> InitializeResponse:
        return await self.sessions.initialize(protocol_version, client_capabilities, client_info)

    async def new_session(
        self, cwd: str, mcp_servers: list[Any] | None = None, **kwargs: Any
    ) -> NewSessionResponse:
        return await self.sessions.new_session(cwd, mcp_servers, **kwargs)

    async def load_session(
        self,
        cwd: str,
        session_id: str,
        mcp_servers: list[Any] | None = None,
        **kwargs: Any,
    ) -> LoadSessionResponse:
        return await self.sessions.load_session(cwd, session_id, mcp_servers, **kwargs)

    async def _attach_owner(self, thread: Thread, session_id: str) -> LoadSessionResponse:
        return await self.sessions.attach_owner(thread, session_id)

    async def _replay_transcript(
        self,
        session_id: str,
        name: str,
        client: Any = None,
        *,
        snapshots: bool | None = None,
        diffs: bool | None = None,
    ) -> None:
        await self.sessions.transcript.replay(
            session_id, name, client, snapshots=snapshots, diffs=diffs
        )

    async def prompt(self, session_id: str, prompt: list[Any], **kwargs: Any) -> PromptResponse:
        instructions = self._manual_compaction_instructions(prompt, kwargs)
        if instructions is not None:
            # Idle owner bridge alone owns the lock and the one-POST budget.
            if session_id in self.sessions.proxies:
                result = await self.sessions.proxies[session_id].request(
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
        if "userText" in options and type(options["userText"]) is not str:
            raise RequestError.invalid_params({"reason": "userText must be a string"})
        display_text = options.get("userText") or self._prompt_text(prompt)
        defer_display = options.get("deferDisplay") is True
        if "selectedExistingFileWrite" in options:
            request = options["selectedExistingFileWrite"]
            if (
                type(request) is not dict
                or set(request) != {"sourceSeq", "sourceMessageId", "resource", "contents"}
                or type(request["sourceSeq"]) is not int
                or any(
                    type(request[key]) is not str
                    for key in ("sourceMessageId", "resource", "contents")
                )
                or prompt
                or set(options) != {"selectedExistingFileWrite"}
            ):
                raise RequestError.invalid_params(
                    {"reason": "Selected write requires exact metadata and no prompt"}
                )
            if session_id in self.sessions.proxies:
                result = await self.sessions.proxies[session_id].request(
                    "prompt", meta=meta, prompt=[]
                )
                return PromptResponse.model_validate(result)
            owner = self._require_session(session_id)
            root_id = self._private_nk_marker()
            if root_id is None or self._private_nk_native_package is None:
                raise RequestError.invalid_params(
                    {"reason": "Selected write requires private N/K owner"}
                )
            controller = self._runtime.controller.get()
            if controller is None or (
                controller is UNBOUND_CONTROLLER and self.sessions.client is None
            ):
                raise RequestError.invalid_params(
                    {"reason": "Selected write requires attached ACP controller"}
                )
            receipt = SelectedWritePlans(self._comms, root_id).submit(
                owner_name=owner,
                source_seq=request["sourceSeq"],
                source_message_id=request["sourceMessageId"],
                resource=request["resource"],
                contents=request["contents"],
            )
            attached = self.sessions.client if controller is UNBOUND_CONTROLLER else controller
            self._selected_write_controllers[(owner, request["sourceSeq"])] = (
                str(receipt["operationId"]),
                attached,
            )
            return PromptResponse(
                stop_reason="end_turn", field_meta={"agentComms": {"selectedWrite": receipt}}
            )
        if options.get("clearQueue") is True:
            # Attachment-only clients clear the owner's queue through the
            # existing prompt channel; no turn is launched.
            if session_id in self.sessions.proxies:
                await self.sessions.proxies[session_id].request("clear_queue")
            else:
                await self.inputs.clear_queued_inputs(session_id)
            return PromptResponse(stop_reason="end_turn")
        try:
            images = self._prompt_images(prompt)
        except ValueError as error:
            raise RequestError.invalid_params({"reason": str(error)}) from error
        if images:
            supported = (
                self.sessions.proxy_image_support.get(session_id, False)
                if session_id in self.sessions.proxies
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
        if session_id in self.sessions.proxies:
            result = await self.sessions.proxies[session_id].request(
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
            self.inputs.send_now(session_id)
            return PromptResponse(stop_reason="end_turn")
        text = self._prompt_text(prompt)
        if (
            session_id in self._active_turns
            and backend.rpc_args_for(self._agent_bin, self._agent_args) is not None
            and self.inputs.backend_inboxes.get(session_id) is not None
            and not text.lstrip().startswith(("@", "#", RELAY_PREFIX))
        ):
            return await self.inputs.accept_followup(
                session_id,
                text=text,
                display_text=display_text,
                delivery=delivery,
                defer_display=defer_display,
                images=images,
            )
        async with self._turn_locks.setdefault(session_id, asyncio.Lock()):
            # A direct ACP prompt owns this one controller. Owner-socket prompts
            # already carry a private subscriber binding (including None when
            # absent); never fall back to a passive ACP client in that case.
            existing = self._runtime.controller.get()
            context = (
                self._runtime.controller.set(self.sessions.client)
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
        return await self.inputs.clear_queued_inputs(session_id)

    async def compact_context(
        self, session_id: str, instructions: str | None = None
    ) -> dict[str, Any]:
        """Use the idle owner bridge; never Pi's implicit retrying compaction."""
        from .manual_compaction_bridge import compact_context

        return await compact_context(self, session_id, instructions)

    def _queue_binding(self, session_id: str) -> dict[str, Any] | None:
        return self.inputs.queue_binding(session_id)

    def _queue_state(self, session_id: str) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
        return self.inputs.queue_state(session_id)

    async def _emit_queue_state(
        self, session_id: str, *, restored: list[str] | None = None, client: Any = None
    ) -> None:
        return await self.inputs.emit_queue_state(session_id, restored=restored, client=client)

    async def _emit_input_started(
        self,
        session_id: str,
        text: str | None,
        input_id: str | None = None,
        queued_item: QueuedInput | None = None,
    ) -> None:
        return await self.inputs.emit_input_started(session_id, text, input_id, queued_item)

    async def _emit_input_disposition(
        self, session_id: str, row: dict[str, Any], client: Any = None
    ) -> None:
        return await self.inputs.emit_input_disposition(session_id, row, client)

    async def _emit_public_input_disposition(
        self, session_id: str, disposition: dict[str, Any], client: Any = None
    ) -> None:
        return await self.inputs.emit_public_input_disposition(session_id, disposition, client)

    async def emit_input_delivery_changed(self, session_id: str) -> None:
        return await self.inputs.emit_input_delivery_changed(session_id)

    async def replay_unknown_inputs(self, session_id: str, client: Any = None) -> None:
        return await self.inputs.replay_unknown_inputs(session_id, client)

    def awaiting_input_keys(self, session_id: str) -> frozenset[str] | None:
        return self.inputs.awaiting_input_keys(session_id)

    async def emit_session_identity(self, session_id: str, name: str, client: Any = None) -> None:
        """Let a subscriber identify its owner before potentially long replay."""
        await (client or self._runtime).session_update(
            session_id=session_id,
            update=AgentMessageChunk(
                session_update="agent_message_chunk",
                content=TextContentBlock(type="text", text=""),
                field_meta=self._session_metadata(name, session_id=session_id),
            ),
        )

    async def _prompt_owned(
        self, session_id: str, prompt: list[Any], *, display_text: str | None = None
    ) -> PromptResponse:
        turn_task = asyncio.current_task()
        assert turn_task is not None
        self._turn_tasks[session_id] = turn_task
        try:
            thread_name = await self.sessions.sync_identity(session_id)
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
                await self.inputs.run_owned_input(
                    session_id,
                    thread_name,
                    agent_task or "",
                    images=images,
                    display_text=display_text,
                )
            elif agent_task:
                await self.inputs.run_owned_input(
                    session_id, thread_name, agent_task, display_text=display_text
                )
            else:
                turn_id = uuid4().hex
                turn_claim = self._comms.begin_turn(thread_name, turn_id, "Waiting for replies")
                self._active_turns[session_id] = turn_id
                try:
                    await self._emit_event(session_id, self._started_event(thread_name, turn_id))
                    await self.inputs.drain_inbox(session_id)
                    await self._collect_replies(session_id, thread_name, sent_seq)
                finally:
                    await self.settle_turn(session_id, thread_name, turn_id, turn_claim)
            self._debug_log("prompt:returning")
            return PromptResponse(stop_reason="end_turn")
        except asyncio.CancelledError:
            self._debug_log("prompt:cancelled")
            await backend.terminate_task_process(turn_task)
            return PromptResponse(stop_reason="cancelled")
        finally:
            if self._turn_tasks.get(session_id) is turn_task:
                self._turn_tasks.pop(session_id, None)
            self.inputs.ensure_live_drain(session_id)

    async def _run_owned_input(
        self,
        session_id: str,
        thread_name: str,
        task: str,
        *,
        images: tuple[Any, ...] = (),
        display_text: str | None = None,
    ) -> None:
        await self.inputs.run_owned_input(
            session_id, thread_name, task, images=images, display_text=display_text
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
            pushed = await self.inputs.drain_count(session_id)
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
        if session_id in self.sessions.proxies:
            await self.sessions.proxies[session_id].request("cancel")
            return
        name = self.sessions.bindings.get(session_id)
        if name and (goal := self._comms.registry.require(name).goal) and goal.active:
            self._comms.update_goal(
                name,
                PausedGoalAction(expect=GoalPrecondition(goal_id=goal.id)),
                actor=OwnerInvocable,
            )
        task = self._turn_tasks.get(session_id)
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            await backend.terminate_task_process(task)
        thread_name = self.sessions.bindings.get(session_id)
        if thread_name:
            self._comms.acknowledge(thread_name)

    async def set_config_option(
        self, config_id: str, session_id: str, value: str | bool, **kwargs: Any
    ) -> SetSessionConfigOptionResponse:
        return await self.config.set_option(config_id, session_id, value)

    async def authenticate(self, method_id: str, **kwargs: Any) -> None:
        raise RequestError.auth_required({"reason": "agent-comms requires no authentication"})

    # ─── Helpers ─────────────────────────────────────────────────────────────

    def _require_session(self, session_id: str) -> str:
        return self.sessions.require(session_id)

    def _private_cursor_scope(self, thread_name: str, session_id: str) -> dict[str, Any] | None:
        root_id = self._private_nk_wire_root_id
        if root_id is None:
            return None
        try:
            owner, epoch = self._comms.registry.live_owner_with_admission(thread_name)
        except (OSError, ValueError):
            return None
        if owner.pid != os.getpid():
            return None
        return {
            "sessionId": session_id,
            "wireRootId": root_id,
            "ownerThread": owner.name,
            "ownerCreatedAt": owner.created_at,
            "ownerPid": owner.pid,
            "ownerEpoch": epoch,
        }

    def _private_cursor_metadata(
        self, thread_name: str, session_id: str, *, defer_busy: bool = False
    ) -> dict[str, Any]:
        """Owner-scoped, ordered informational cursor for trusted ACP attach.

        Every status (including none/unavailable) advances the local projection
        revision before an async update can be delayed. Only a trusted new/load
        response or owner-ready may bind a client to this scope; callbacks must
        never establish authority. Neither cursor nor ACK proves consumption.
        """
        root_id = self._private_nk_wire_root_id
        if root_id is None:
            return {}
        revision = self._private_cursor_revisions.get(session_id, 0) + 1
        self._private_cursor_revisions[session_id] = revision
        scope = self._private_cursor_scope(thread_name, session_id)
        result: dict[str, Any] = {
            "version": 1,
            "scope": scope,
            "revision": revision,
            "status": "unavailable",
        }
        if scope is None:
            return result
        try:
            with MutationStore(str(self._comms.root / "coordination.sqlite3")) as store:
                bus = MessageBus(
                    self._comms.root / "bus.jsonl",
                    self._comms.registry,
                    private_response_writes=True,
                )
                cursor = read_current_native_cursor(
                    bus, store, wire_root_id=root_id, owner_name=thread_name
                )
        except BlockingIOError:
            # A writer holding a nonblocking observation lock did not invalidate
            # the last observation. On periodic refresh, try again next poll
            # instead of making the UI alternate between proof and unavailable.
            # A changed/unknown owner still invalidates immediately; a trusted
            # load without an observation still reports unavailable.
            current_scope = self._private_cursor_scope(thread_name, session_id)
            if defer_busy and current_scope == scope:
                raise
            result["scope"] = current_scope
            return result
        except (OSError, ValueError, sqlite3.Error, CoordinationError):
            cursor = None
            unavailable = True
        else:
            unavailable = False
        # A replacement during the read invalidates even a coherent old row.
        # Fail closed for the newly observed incarnation; a later trusted
        # snapshot may show its own current cursor.
        current_scope = self._private_cursor_scope(thread_name, session_id)
        if current_scope != scope:
            result["scope"] = current_scope
            return result
        if unavailable:
            return result
        if cursor is None:
            result["status"] = "none"
        else:
            result.update(
                status="proven" if cursor.injected_seq else "coverage_only",
                **asdict(cursor),
            )
        return result

    async def _publish_private_cursor(
        self, session_id: str, thread_name: str, *, selected_status: str | None = None
    ) -> None:
        """Publish observed owner transitions even when no native input was sent.

        A stopped/re-admitted owner or a new admission with no current cursor
        must invalidate a previously displayed proof. This is only projection
        metadata: it never selects, sends, acknowledges, or retries an input.
        """
        try:
            cursor = self._private_cursor_metadata(thread_name, session_id, defer_busy=True)
        except BlockingIOError:
            return  # Read contention; the next poll refreshes this observation.
        signature = json.dumps(
            {key: value for key, value in cursor.items() if key != "revision"},
            sort_keys=True,
        )
        if selected_status is None and self._private_cursor_announced.get(session_id) == signature:
            return
        fields: dict[str, Any] = {"privateNativeCursor": cursor}
        if selected_status is not None:
            fields["lastSelectedCursorStatus"] = selected_status
        try:
            await self._runtime.session_update(
                session_id=session_id,
                update=SessionInfoUpdate(
                    session_update="session_info_update", field_meta={"agentComms": fields}
                ),
            )
        except (OSError, RuntimeError):
            return  # A disconnected client can read a fresh trusted load later.
        self._private_cursor_announced[session_id] = signature

    def _session_metadata(
        self, thread_name: str, *, session_id: str | None = None
    ) -> dict[str, Any]:
        return self.sessions.metadata(thread_name, session_id=session_id)

    def _session_runtime_metadata(self, thread_name: str, session_id: str) -> dict[str, Any]:
        queue_binding, queue_state = self.inputs.queue_state(session_id)
        return {
            "queueBinding": queue_binding,
            "queueState": queue_state,
            **(
                {"privateNativeCursor": self._private_cursor_metadata(thread_name, session_id)}
                if self._private_nk_wire_root_id is not None
                else {}
            ),
        }

    async def _refresh_auth_models(self) -> None:
        await self.config.refresh_auth_models()

    async def _config_options(self, thread_name: str) -> list[Any]:
        return await self.config.options(thread_name)

    async def _sync_session_identity(self, session_id: str) -> str:
        return await self.sessions.sync_identity(session_id)

    def _ensure_live_drain(self, session_id: str) -> None:
        return self.inputs.ensure_live_drain(session_id)

    async def _drain_inbox(self, session_id: str) -> int:
        return await self.inputs.drain_inbox(session_id)

    async def _sync_thread_config(self, session_id: str) -> None:
        await self.config.sync_thread(session_id)

    async def _sync_goal_execution(self, session_id: str, thread_name: str) -> None:
        event = self._comms.goal_changed(
            thread_name, self._goal_execution_signatures.get(session_id)
        )
        if event is None:
            return
        await self._emit_event(session_id, event)
        self._goal_execution_signatures[session_id] = event.signature

    def _private_session_mode(self) -> bool:
        marker = self._private_nk_marker()
        if marker is None:
            return False
        if self._private_nk_wire_root_id != marker or self._private_nk_native_package is None:
            raise PublicationActivationBlocked(
                "private N/K ACP session requires explicit matching root and package"
            )
        return True

    def _private_nk_marker(self) -> str | None:
        """Distinguish exact legacy metadata from a guarded private marker.

        Both protocols use bus_meta.json. A mere file-existence test would
        reject ordinary public ACP roots; an ambiguous/damaged marker must not
        fall back to their legacy ACK path.
        """
        marker_path = self._comms.root / "bus_meta.json"
        with _store_lock(self._comms.bus._path):
            if marker_path.is_symlink():
                raise IdentityConflict("ACP bus marker is redirected")
            if not marker_path.exists():
                return None
            try:
                metadata = json.loads(marker_path.read_text(), object_pairs_hook=unique_wire_object)
            except (OSError, ValueError, UnicodeError) as error:
                raise IdentityConflict("ACP bus marker is invalid") from error
            if type(metadata) is not dict:
                raise IdentityConflict("ACP bus marker is not an object")
            if "writer_protocol_version" in metadata:
                guarded = self._comms.bus._private_marker_unlocked()
                return str(guarded["wire_root_id"])
            if (
                set(metadata) != {"last_seq"}
                or type(metadata["last_seq"]) is not int
                or metadata["last_seq"] < 0
            ):
                raise IdentityConflict("ACP public bus marker has an unknown protocol")
            return None

    async def _drain_private_nk(self, session_id: str, wire_root_id: str) -> int:
        """Selected private wake for this ACP session, never legacy inbox ACK.

        This explicitly configured path reuses the reviewed one-shot native
        reservation/send boundary. No schema/participant is installed here;
        both must already belong to the same private root. A failing or
        uncertain native turn propagates and cannot be replayed by a drain.
        """
        package = self._private_nk_native_package
        if package is None or self._private_nk_wire_root_id != wire_root_id:
            raise PublicationActivationBlocked(
                "private N/K ACP requires an explicit matching root and native package"
            )
        _preflight(self._comms.root, wire_root_id, package, True)
        thread_name = await self.sessions.sync_identity(session_id)
        # Registry admission may change without session/new or session/load.
        # Publish the observed status even when stopped, busy, or no-wake;
        # callbacks may only invalidate a prior client binding, not replace it.
        await self._publish_private_cursor(session_id, thread_name)
        if not self.inputs.auto_wake or not self._runtime_enabled:
            return 0  # Explicitly disabled: no legacy path or ACK fallback.
        if self._comms.registry.status(thread_name).stopped:
            return 0
        if (
            session_id in self._active_turns
            or session_id in self._turn_tasks
            or session_id in self.inputs.backend_inboxes
        ):
            return 0  # Never overlap the ACP owner session's running turn.
        owner = self._comms.registry.require(thread_name)
        if owner.pid != os.getpid():
            raise IdentityConflict("private N/K ACP recipient is not this process owner")
        if owner.active_turn is not None:
            raise StaleFence("private N/K ACP owner is busy")
        bus = MessageBus(
            self._comms.root / "bus.jsonl", self._comms.registry, private_response_writes=True
        )
        with MutationStore(str(self._comms.root / "coordination.sqlite3")) as store:
            _accept_visible_initials(
                bus,
                wire_root_id,
                store,
                stable_thread_lookup(owner.created_at),
                0,
                owner_name=owner.name,
            )
        plans = SelectedWritePlans(self._comms, wire_root_id)

        def check_plan_controller(claim: WakeClaim, owner: Thread, operation_id: str) -> None:
            bound = self._selected_write_controllers.get((owner.name, claim.wire_seq))
            if bound is None or bound[0] != operation_id:
                raise IdentityConflict("Selected write original controller is no longer bound")
            controller = bound[1]
            if isinstance(controller, SocketClient):
                if not self._runtime.is_controller(session_id, controller):
                    raise IdentityConflict("Selected write controller disconnected")
            elif controller is not self.sessions.client or controller is None:
                raise IdentityConflict("Selected write ACP controller changed")

        def load_plan(claim: WakeClaim, owner: Thread, epoch: int) -> PlannedWrite | None:
            plan = plans.load(claim, owner, epoch)
            if plan is not None:
                check_plan_controller(claim, owner, plan.operation_id)
            return plan

        def applied_plan(claim: WakeClaim, owner: Thread, operation_id: str) -> None:
            plans.applied(claim, owner, operation_id)
            self._selected_write_controllers.pop((owner.name, claim.wire_seq), None)

        result = await run_one_sealed_claim(
            self._comms.root,
            wire_root_id=wire_root_id,
            owner_name=thread_name,
            native_package=package,
            selected_write_plan_loader=load_plan,
            selected_write_plan_check=check_plan_controller,
            selected_write_plan_applied=applied_plan,
            **(
                {"selected_tool_intent": self._private_selected_tool_intent}
                if self._private_selected_tool_intent is not None
                else {}
            ),
        )
        if result is None:
            # N (or absent-audience) rows prove coverage, not an injected
            # input. Extend only an existing current epoch or an all-N prefix;
            # old-epoch Pi evidence cannot initialize this cursor on reconnect.
            try:
                with MutationStore(str(self._comms.root / "coordination.sqlite3")) as store:
                    person = store.participant(stable_thread_lookup(owner.created_at))
                    epoch = self._comms.registry.snapshot().admission_generations[thread_name]
                    cursor = advance_current_native_cursor(
                        bus,
                        store,
                        wire_root_id=wire_root_id,
                        owner=owner,
                        owner_admission_epoch=epoch,
                        owner_generation=person.generation,
                        committed_input_id=None,
                    )
            except (OSError, ValueError, sqlite3.Error, CoordinationError, KeyError):
                cursor = None  # projection unavailable; no claim or model retry
            if cursor is not None:
                await self._publish_private_cursor(session_id, thread_name)
        else:
            # A disconnected client must not turn a settled claim into an
            # apparent model failure. Reconnect reads the same durable row.
            await self._publish_private_cursor(
                session_id, thread_name, selected_status=result.cursor_status
            )
        return int(result is not None)

    async def _drain_owned_inbox(self, session_id: str) -> int:
        return await self.inputs.drain_owned_inbox(session_id)

    def _forget_direct_interrupt(self, session_id: str, turn: ScheduledTurn) -> None:
        return self.inputs.forget_direct_interrupt(session_id, turn)

    def _schedule_wake(self, session_id: str) -> None:
        return self.inputs.schedule_wake(session_id)

    def _schedule_goal(self, session_id: str) -> None:
        """Only the existing thread owner may schedule another goal turn."""
        if (
            self.inputs.closing
            or session_id in self._turn_tasks
            or session_id in self.inputs.backend_inboxes
            or session_id in self._active_turns
        ):
            return
        if (wake := self.inputs.wake_tasks.get(session_id)) is not None and not wake.done():
            return
        thread = self._comms.registry.require(self._require_session(session_id))
        if pending := self.inputs.pending_turns.get(session_id):
            fresh = [
                turn
                for turn in pending
                if turn.still_current_interrupt(thread.goal)
                and (
                    turn.direct_interrupt_goal_id is None
                    or (
                        (row := self.inputs.dispositions.get(turn.direct_interrupt_input_key or ""))
                        is not None
                        and row["status"] == "unknown"
                        and row["native_id"] is None
                    )
                )
            ]
            for turn in pending:
                if turn not in fresh:
                    self.inputs.forget_direct_interrupt(session_id, turn)
            if fresh:
                self.inputs.pending_turns[session_id] = fresh
                return
            self.inputs.pending_turns.pop(session_id, None)
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
                if generation is None or not generation.lifecycle.ready:
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
            self.inputs.pending_turns.setdefault(session_id, []).append(
                ScheduledTurn(GOAL_CONTINUE_PROMPT, goal_id=goal.id)
            )
            self.inputs.schedule_wake(session_id)

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
            SetGoalAction(text=text, expect=GoalPrecondition(expected_owner_pid=os.getpid())),
            actor=OwnerInvocable,
            owner_store=self._open_goal_store(),
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
            EditGoalAction(
                text=text,
                expect=GoalPrecondition(
                    goal_id=goal_id,
                    expected_goal=goal,
                    expected_owner_pid=os.getpid(),
                ),
            ),
            actor=OwnerInvocable,
        )
        assert edited is not None
        await self.config.sync_thread(session_id)
        return edited

    async def update_goal(
        self, session_id: str, status: str, goal_id: str, expected_revision: int
    ) -> Goal | None:
        """Apply an explicit UI pause, resume, or clear through the current owner."""
        action = GoalAction.decode(status)
        if not issubclass(action, OwnerControlInvocable):
            raise ValueError("Goal updates support only active, paused, or clear.")
        name = self._require_session(session_id)
        goal = self._comms.registry.require(name).goal
        if goal is None or goal.id != goal_id or goal.revision != expected_revision:
            raise ValueError("The goal changed; refresh its state before updating.")
        try:
            updated = self._comms.update_goal(
                name,
                action(
                    expect=GoalPrecondition(
                        goal_id=goal_id,
                        expected_goal=goal,
                        expected_owner_pid=os.getpid(),
                    )
                ),
                actor=OwnerInvocable,
                owner_store=self._open_goal_store() if action.owner_grant else None,
            )
        finally:
            # Resume can discover that a paused attempt failed. Publish the
            # reconciled BLOCKED state even when the action returns an error.
            await self.config.sync_thread(session_id)
        if action.schedules_goal:
            self._schedule_goal(session_id)
        return updated

    async def retry_goal(self, session_id: str, goal_id: str, expected_revision: int) -> Goal:
        """Record an explicit UI retry in the executing owner's private ledger."""
        name = self._require_session(session_id)
        thread = self._comms.registry.require(name)
        goal = thread.goal
        if goal is None or goal.id != goal_id or goal.revision != expected_revision:
            raise ValueError("The blocked goal changed; refresh its state.")
        if self._pending_goal_origins.get(name) == goal_id:
            raise ValueError("Wait for the goal origin turn to finish.")
        resumed = self._comms.update_goal(
            name,
            RetryGoalAction(
                expect=GoalPrecondition(
                    goal_id=goal_id,
                    expected_goal=goal,
                    expected_owner_pid=os.getpid(),
                )
            ),
            actor=OwnerInvocable,
            owner_store=self._open_goal_store(),
        )
        assert resumed is not None
        # READY records the accepted owner decision even during an unrelated
        # turn. The scheduler's existing busy fences defer launch until that
        # turn finishes; reserved/claimed attempts remain unretryable above.
        self._schedule_goal(session_id)
        await self._sync_goal_execution(session_id, name)
        return resumed

    async def _drain_count(self, session_id: str) -> int:
        return await self.inputs.drain_count(session_id)

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
            elif controller is self.sessions.client:
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
        direct_interrupt_goal_id: str | None = None,
        direct_interrupt_goal_revision: int | None = None,
        direct_interrupt_wait_id: str | None = None,
        direct_interrupt_input_key: str | None = None,
        direct_interrupt_ticket: str | None = None,
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
        direct_interrupt = direct_interrupt_goal_id is not None
        if direct_interrupt:
            tickets = self.inputs.direct_interrupt_tickets.get(session_id, {})
            if (
                direct_interrupt_input_key is None
                or direct_interrupt_ticket is None
                or tickets.get(direct_interrupt_input_key) != direct_interrupt_ticket
            ):
                return
            tickets.pop(direct_interrupt_input_key, None)  # one admission, one turn at most
            # A typed interruption must be one NEW exact direct input whose
            # goal revision and wait ID were captured AT DISPATCH; any change
            # after dispatch denies without retry. Benign bumps before
            # dispatch are handled by the dispatcher's fresh rebind.
            aliases = self._comms.registry.aliases_for(thread_name)
            if (
                autonomous_goal
                or original_owner_input
                or dependency_wait_id is not None
                or goal is None
                or not goal.active
                or goal.id != direct_interrupt_goal_id
                or goal.revision != direct_interrupt_goal_revision
                or (wait.wait_id if wait else None) != direct_interrupt_wait_id
                or len(origins) != 1
                or origins[0].seq <= 0
                or origins[0].target not in aliases
                or direct_interrupt_input_key is None
                or original_keys
                or direct_interrupt_input_key
                != self.inputs.dispositions.bus_key(origins[0], thread)
            ):
                return
            row = self.inputs.dispositions.get(direct_interrupt_input_key)
            if (
                row is None
                or row["status"] != "unknown"
                or row["native_id"] is not None
                or row["sequence"] != origins[0].seq
            ):
                return
        if (
            wait is not None
            and not original_owner_input
            and not direct_interrupt
            and wait.wait_id != dependency_wait_id
        ):
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
        if goal is not None and goal.active and not direct_interrupt:
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
                raise _goal_attempt_unavailable()
            admission = self._comms.registry.snapshot().admission_generations[thread_name]
            with _store_lock(self._comms._wire_lock_path):
                generation = store.snapshot(goal.id)
                if generation is None or not generation.lifecycle.ready:
                    if autonomous_goal:
                        return
                    raise _goal_attempt_unavailable()
                try:
                    grant = self._ready_goal_grant_locked(thread, admission, store, generation)
                    reservation = store.reserve(goal.id, generation.number, ready_grant=grant)
                    goal_permit = store.claim_launch(reservation)
                except GoalAttemptError as error:
                    if autonomous_goal:
                        return
                    raise _goal_attempt_unavailable() from error
        self.sessions.bindings[session_id] = thread_name
        # Error-display deduplication belongs to one backend turn, not a session.
        self._emitted_errors.pop(session_id, None)
        turn_id = uuid4().hex
        routing = TurnRouting(
            origins, MessageRoute(thread_name, reply_targets) if reply_targets else None
        )
        checkpoint = self._comms.transcript_checkpoint(thread_name)
        turn_claim = self._comms.begin_turn(thread_name, turn_id, task[:80], routing)
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
                    key = self.inputs.dispositions.bus_key(origin, thread)
                    if self.inputs.dispositions.get(key) is None:
                        self.inputs.dispositions.record(
                            key,
                            seq=origin.seq,
                            owner=thread_name,
                            admission=snapshot.admission_generations[thread_name],
                            target=origin.target,
                            text=ScheduledTurn.incoming(origin, aliases=snapshot.aliases).prompt,
                        )
                    original_keys = (*original_keys, key)
        self.inputs.turn_input_keys[session_id] = set(original_keys)
        self.inputs.steering_input_keys[session_id] = {}
        self.inputs.steering_origins[session_id] = {}
        self.inputs.steering_goal_ids[session_id] = {}

        passive_frame = ""
        passive_sources: tuple[tuple[int, str, str], ...] = ()
        channel_batch = (
            len(origins) > 1
            and len({origin.seq for origin in origins}) == len(origins)
            and all(origin.seq > 0 and is_channel_target(origin.target) for origin in origins)
            and original_keys
            == tuple(self.inputs.dispositions.bus_key(origin, thread) for origin in origins)
            # The durable admission owns the exact prompt, including the
            # names resolved at admission. Re-deriving it here can drift if
            # a recipient was renamed before or after inbox draining.
            and (admitted_texts := self.inputs.dispositions.source_texts(original_keys)) is not None
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
                self._comms.maintenance.assert_open_unlocked()
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
                # A parked-goal DM owns no goal attempt. A fresh owner input
                # may join that SAME interruption, not borrow or retry the goal.
                interrupt_scope_current = (
                    direct_interrupt
                    and current_goal is not None
                    and current_goal.active
                    and current_goal.id == direct_interrupt_goal_id
                    and current_goal.revision == direct_interrupt_goal_revision
                    and (current_wait.wait_id if current_wait else None) == direct_interrupt_wait_id
                )
                owner_interrupt_followup = False
                input_permit = goal_permit
                admitted_goals = self.inputs.steering_goal_ids.get(session_id, {})
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
                        owner_interrupt_followup = interrupt_scope_current and goal_ok
                        goal_ok = owner_interrupt_followup
                keys = (
                    original_keys
                    if public_id is None
                    else (
                        (key,)
                        if (
                            key := self.inputs.steering_input_keys.get(session_id, {}).get(
                                public_id
                            )
                        )
                        else ()
                    )
                )
                if owner_interrupt_followup:
                    # The permitless exception requires this exact fresh ACP
                    # admission; an absent/foreign mapping cannot skip binding.
                    owner_interrupt_followup = keys == (f"acp:{public_id}",)
                    goal_ok = owner_interrupt_followup
                interrupt_ok = (
                    interrupt_scope_current
                    and public_id is None
                    and len(direct_origins) == 1
                    and len(keys) == 1
                    and current is not None
                    and keys[0] == direct_interrupt_input_key
                    and keys[0] == self.inputs.dispositions.bus_key(direct_origins[0], current)
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
                if owner_ok and public_id is None and passive_frame:
                    assert current is not None
                    try:
                        owner_ok = self.inputs.passive_awareness.still_current(
                            current,
                            snapshot,
                            self._comms.channel_catalog.targets_for(current.tags),
                            passive_sources,
                        )
                    except (OSError, TypeError, ValueError):
                        owner_ok = False  # No stale advisory frame crosses native start.
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
                        or interrupt_ok
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
                            and (
                                original_owner_input
                                or dependency_wait_id is not None
                                or interrupt_ok
                            )
                        )
                    )
                )
                selected_admission = (
                    self.inputs.selected_summary_admissions.get(session_id)
                    if public_id is None
                    else None
                )
                if allowed and selected_admission is None:
                    from .compaction_send_admission import native_input_admitted

                    # Under the same wire lock as the owner commit. A selected
                    # row blocks this ordinary path regardless of its status.
                    allowed = current is not None and native_input_admitted(
                        self._comms.root, current.session_file
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
                if not allowed and selected_admission is not None:
                    selected_admission.invalidate()  # No later owner/turn ABA can revive it.
                if allowed and selected_admission is not None:
                    # The only selected bypass is a post-fsync-ACK ephemeral
                    # one-shot, consumed at this same durable native-ID bind
                    # point under the wire lock, before any stdin.write. No
                    # admission is reconstructed from SQLite.
                    if (
                        current is None
                        or current.session_file is None
                        or len(keys) != 1
                        or already_bound
                    ) or (revision := backend._session_revision(current.session_file)) is None:
                        selected_admission.invalidate()
                        allowed = False
                    else:
                        original = self.inputs.dispositions.get(keys[0])
                        if original is None or type(original["source_text"]) is not str:
                            selected_admission.invalidate()
                            allowed = False
                        else:
                            digest = hashlib.sha256(sent_text.encode()).hexdigest()
                            original_digest = hashlib.sha256(
                                original["source_text"].encode()
                            ).hexdigest()
                            identity = SelectedAdmissionIdentity(
                                owner_name=canonical,
                                owner_pid=current.pid,
                                owner_created_at=float(current.created_at).hex(),
                                turn_id=turn_id,
                                ingress_key=keys[0],
                                admission_generation=snapshot.admission_generations[canonical],
                                correction_witness=(
                                    f"{snapshot.admission_generations[canonical]}:{digest}"
                                ),
                                input_sha256=digest,
                                original_sha256=original_digest,
                                reserved_revision=selected_admission._identity.reserved_revision,
                                session_revision=revision,
                            )
                            try:
                                self.inputs.dispositions.compaction_rows(
                                    current, keys[0], self.inputs
                                )
                            except RelationViolationError:
                                selected_admission.invalidate()
                            allowed = selected_admission.consume_bound_original(
                                wire_root=self._comms.root,
                                session_file=current.session_file,
                                identity=identity,
                                native_id=native_id,
                                sent_text=sent_text,
                                dispositions=self.inputs.dispositions,
                            )
                elif allowed:
                    for key in keys:
                        row = self.inputs.dispositions.get(key)
                        if (
                            row is None
                            or row["status"] != "unknown"
                            or row["admission"] != snapshot.admission_generations[canonical]
                            or not (
                                row["native_id"] == native_id
                                and row["turn_id"] == turn_id
                                and row["sent_text"] == sent_text
                                if already_bound
                                else self.inputs.dispositions.bind(
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
                if (
                    allowed
                    and current_wait is not None
                    and not interrupt_ok
                    and not owner_interrupt_followup
                ):
                    allowed = self._comms.consume_goal_wait(canonical, current_wait.wait_id)
                if allowed:
                    if public_id is None:
                        display = original_display
                        input_origins = origins
                    else:
                        row = self.inputs.dispositions.get(keys[0]) if keys else None
                        display = row["source_text"] if row is not None else sent_text
                        origin = self.inputs.steering_origins.get(session_id, {}).get(public_id)
                        input_origins = (origin,) if origin is not None else ()
                    self._comms.record_input_display(
                        native_id,
                        display,
                        sent_text=sent_text,
                        routing=TurnRouting(input_origins, None) if input_origins else None,
                    )
                yield True if allowed else None if defer_for_goal else False

        # Backend recognizes this exact callback as already owning the wire
        # lock; arbitrary callbacks receive its outer send-boundary lock.
        send_boundary._maintenance_wire_locked = True  # type: ignore[attr-defined]

        def native_start(public_id: str | None, native_id: str, sent_text: str) -> bool:
            keys = (
                original_keys
                if public_id is None
                else (
                    (key,)
                    if (key := self.inputs.steering_input_keys.get(session_id, {}).get(public_id))
                    else ()
                )
            )
            return input_keys_valid(public_id, keys, sent_text) and all(
                self.inputs.dispositions.started(
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
            if direct_interrupt:
                task = (
                    f"Persistent goal {goal.id} is parked for this ordinary direct-message "
                    "interruption. This is NOT a goal attempt or declared dependency reply. "
                    "Respond to this message first; do not call comms_goal merely to finish "
                    "the DM, report goal progress, clear its standby wait, or retry an UNKNOWN "
                    "input. The goal remains separately scheduled. Current goal state for "
                    "answering questions about it only (verify live project state before "
                    "reporting current PR status):\n"
                    f"Goal status: {goal.status}; revision: {goal.revision}\n"
                    f"Objective: {goal.text}\nProgress: {goal.progress}\n\n" + task
                )
            else:
                task = (
                    f"Persistent goal {goal.id}: {goal.text}\nProgress: {goal.progress}\n"
                    "Work toward this goal while respecting follow-up instructions. "
                    "Use comms_goal with this goal_id to record useful progress. "
                    "Set status completed "
                    "only after verifying success, blocked when you need user input, or active "
                    "to continue useful work in another turn. When waiting for delegated work, "
                    "set status standby with explicit wait_for thread names "
                    "and explain what you need. "
                    "The goal stays active without polling; a direct message from a named "
                    "dependency "
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
        terminal_failure: dict[str, Any] = {}
        successful_tool_observed = False
        goal_attempt_resolved = False
        originated_goal_ids: set[str] = set()
        originated_attempts: dict[str, LaunchPermit] = {}
        unattributed_usage: list[tuple[str, dict[str, Any]]] = []
        settled = False
        terminal_fence: FinishedTurnFence | None = None
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
        self.inputs.backend_inboxes[session_id] = backend_inbox
        self._active_turns[session_id] = turn_id
        if original_owner_input and original_keys:
            self.inputs.turn_original_input_keys[session_id] = tuple(original_keys)
            self.inputs.turn_input_text[session_id] = original_display or task
        try:
            await self._emit_event(session_id, self._started_event(thread_name, turn_id))
            await self.inputs.emit_input_delivery_changed(session_id)
            await self.inputs.drain_inbox(session_id)
            from .compaction_publication import publish_pending_local

            # Existing local ACP owner session only. If delivery is uncertain,
            # the keyed metadata remains pending; never invent a bus recipient.
            await publish_pending_local(self, session_id, thread_name)
            # ACP delivery/ACK/UI updates above are not model context. This
            # bounded projection is prepared ONLY inside an already authorized
            # natural turn, from a separate owner-bound source cursor. It never
            # advances that cursor or creates a wake, claim or native receipt.
            if backend.rpc_args_for(self._agent_bin, self._agent_args) is not None:
                with _store_lock(self._comms._wire_lock_path):
                    snapshot = self._comms.registry.snapshot()
                    current_thread = snapshot.threads.get(thread_name)
                    if (
                        current_thread is not None
                        and current_thread.created_at == thread.created_at
                        and snapshot.admission_generations.get(thread_name) == turn_admission
                        and current_thread.active_turn is not None
                        and current_thread.active_turn.id == turn_id
                    ):
                        try:
                            passive_frame = self.inputs.passive_awareness.frame(
                                current_thread,
                                snapshot,
                                self._comms.channel_catalog.targets_for(current_thread.tags),
                            )
                            if passive_frame:
                                passive_sources = self.inputs.passive_awareness.sources(
                                    current_thread
                                )
                                if passive_sources:
                                    task += passive_frame
                                else:
                                    passive_frame = ""
                        except (OSError, TypeError, ValueError):
                            # Before native start, omit the optional projection;
                            # the already-authorized owner task remains intact.
                            passive_frame = ""
                            passive_sources = ()
            if (
                self._adaptive_compaction_enabled
                and original_owner_input
                and len(original_keys) == 1
                and thread.session_file is not None
                and backend.rpc_args_for(self._agent_bin, self._agent_args) is not None
            ):
                from .owner_compaction_adaptive import maybe_compact_owner_turn

                def admit_original(admission: SelectedSummaryAdmission) -> None:
                    self.inputs.selected_summary_admissions[session_id] = admission

                try:
                    committed = await maybe_compact_owner_turn(
                        self._comms.registry,
                        self._agent_bin,
                        thread_name,
                        turn_id,
                        self._comms.agent_info_of(thread_name),
                        original_keys[0],
                        self._persistent_backends.setdefault(
                            session_id, backend.PersistentPiSession()
                        ),
                        summary_strategy=self._adaptive_summary_strategy,
                        input_text=task,
                        on_admission=admit_original,
                        future_queue=self.inputs,
                    )
                except Exception:
                    # A selected adaptive operation may already have paid or
                    # written. Do not turn a fault into ordinary input fallback.
                    if goal is not None and goal.active:
                        self._comms.block_goal_after_failed_turn(
                            thread_name,
                            started_goal=goal,
                            expected_worktree=thread.worktree,
                            diagnostic=(
                                "Adaptive native compaction did not establish a "
                                "safe outcome; inspect the exact commit journal."
                            ),
                        )
                    raise
                if committed:
                    # Local metadata-only outbox; uncertain subscriber delivery
                    # leaves its exact row pending, never broadcasts a summary.
                    await publish_pending_local(self, session_id, thread_name)
            session_file = thread.session_file
            fork_session = False
            if not session_file and thread.parent:
                session_file = self._comms.registry.require(thread.parent).session_file
                fork_session = bool(session_file)
            image_options: dict[str, Any] = {"images": images} if images else {}
            agent = self

            class TurnEventPublication(MroDispatch):
                @handles(events.InputStarted, events.Done, events.StreamSettled)
                async def sync_goals(self, event: events.InputStarted) -> None:
                    await agent._sync_goal_execution(session_id, thread_name)

                @handles(events.ToolEnd)
                async def tool_result(self, event: events.ToolEnd) -> None:
                    await agent._sync_goal_execution(session_id, thread_name)
                    sent = await asyncio.to_thread(
                        agent._comms.sent_tool_message,
                        event.name,
                        event.output,
                        bool(event.ok),
                    )
                    if sent is not None:
                        await agent._emit_event(
                            session_id,
                            SentTranscriptUpdate(
                                text=sent.body, route=MessageRoute(sent.sender, (sent.target,))
                            ),
                        )

            class TurnEventConsumer(events.AgentEventConsumer):
                @property
                def comms(self) -> Comms:
                    return agent._comms

                @property
                def thread_name(self) -> str:
                    return thread_name

                def update_activity(self, state: ActivityState, detail: str) -> None:
                    update_turn_activity(state, detail)

                @handles(events.SteeringInterrupted)
                async def steering_interrupted(self, event: events.SteeringInterrupted) -> None:
                    reply_parts.clear()

                @handles(events.InputRefused)
                async def input_refused(self, event: events.InputRefused) -> None:
                    await agent.inputs.input_refused(session_id, event.id)

                @handles(events.ProviderUsage)
                async def provider_usage(self, event: events.ProviderUsage) -> None:
                    response_id = str(event.response_id)
                    usage = event.usage
                    current_goal = agent._comms.registry.require(thread_name).goal
                    current_goal_id = current_goal.id if current_goal is not None else None
                    permit = originated_attempts.get(current_goal_id or "")
                    if (
                        permit is None
                        and goal_permit is not None
                        and (current_goal_id == goal_permit.reservation.goal_id)
                    ):
                        permit = goal_permit
                    if permit is not None:
                        assert agent._goal_store is not None
                        agent._goal_store.record_provider_usage(permit, response_id, usage)
                    else:
                        unattributed_usage.append((response_id, usage))

                @handles(events.CompactionStart)
                async def compaction_started(self, event: events.CompactionStart) -> None:
                    nonlocal compaction_resume_activity
                    if compaction_resume_activity is None:
                        current_activity = agent._comms.activity_of(thread_name)
                        compaction_resume_activity = (
                            current_activity.state,
                            current_activity.detail,
                        )
                    agent._comms.set_activity(
                        thread_name, ActivityState.WORKING, "Compacting context"
                    )

                @handles(events.CompactionEnd)
                async def compaction_ended(self, event: events.CompactionEnd) -> None:
                    nonlocal compaction_resume_activity
                    if compaction_resume_activity is not None:
                        agent._comms.set_activity(thread_name, *compaction_resume_activity)
                        compaction_resume_activity = None

                @handles(events.CompactionEvent)
                async def invalidate_context(self, event: events.CompactionEvent) -> None:
                    info = agent._comms.agent_info_of(thread_name)
                    agent._comms.set_agent_info(
                        thread_name,
                        model=info.model if info else None,
                        session_name=info.session_name if info else None,
                        context_used=None,
                        context_size=info.context_size if info else None,
                    )

                @handles(events.Chunk)
                async def chunk(self, event: events.Chunk) -> None:
                    if reply_targets:
                        reply_parts.append(event.text)

                @handles(events.CommittedProgress)
                async def committed_progress(self, event: events.CommittedProgress) -> None:
                    if reply_targets:
                        progress = event.text
                        if progress and "".join(reply_parts) == progress:
                            # Pi committed this assistant message before tool work.
                            # Publish it once as visible, non-waking progress; the
                            # final reply contains only subsequent assistant text.
                            for target in reply_targets:
                                agent._comms.send(thread_name, target, progress, notice=True)
                            reply_parts.clear()

                @handles(events.Done)
                async def done(self, event: events.Done) -> events.Done:
                    nonlocal terminal_failure, terminal_ok
                    terminal_failure = asdict(event)
                    unknown_attempts = any(
                        agent.inputs.dispositions.status(key) != "started"
                        for key in agent.inputs.turn_input_keys.get(session_id, set())
                    )
                    if event.ok is True and (
                        agent.inputs.forwarded_inputs.get(session_id) or unknown_attempts
                    ):
                        # A final assistant stop can prove the original turn,
                        # not an ACKed follow-up lacking its own user start.
                        event = replace(
                            event,
                            ok=False,
                            text=(
                                "An identified follow-up input was not started; "
                                "inspect local diagnostics."
                            ),
                        )
                    if terminal_ok is not None:
                        terminal_ok = False
                    elif event.ok is True:
                        terminal_ok = True
                    else:
                        terminal_ok = False
                    if (
                        goal is not None
                        and goal.active
                        and (not direct_interrupt)
                        and (agent._comms.registry.require(thread_name).worktree == thread.worktree)
                    ):
                        current_goal = agent._comms.registry.require(thread_name).goal
                        failed = not event.ok
                        # An RPC stream can settle and exit successfully after
                        # accepting a user prompt without assistant output or tool
                        # activity. This is not a productive goal turn.
                        empty_success = (
                            not failed
                            and not successful_tool_observed
                            and not str(event.text or "").strip()
                        )
                        if (
                            current_goal
                            and current_goal.id == goal.id
                            and current_goal.active
                            and (failed or empty_success)
                        ):
                            # Block the latest same-ID goal under the wire lock,
                            # retaining any newer progress from a concurrent update.
                            agent._comms.block_goal_after_failed_turn(
                                thread_name,
                                started_goal=goal,
                                expected_worktree=thread.worktree,
                                diagnostic=(
                                    "Backend turn failed; "
                                    "inspect local diagnostics before resuming."
                                    if failed
                                    else "Backend reported success without assistant output "
                                    "or tool activity; inspect the session before resuming."
                                ),
                            )
                    return event

                @handles(events.InputStarted)
                async def input_started(self, event: events.InputStarted) -> None:
                    await agent.inputs.input_started(
                        session_id, event.id, original_keys, initial_display_text
                    )

                @handles(events.SettingChangeResult)
                async def setting_result(self, event: events.SettingChangeResult) -> None:
                    agent.config.setting_requests.resolve(event)

                async def before_agent_info(self, event: events.AgentInfo) -> None:
                    session_file = event.session_file
                    if (
                        session_file
                        and agent._comms.registry.require(thread_name).session_file != session_file
                    ):
                        agent._comms.attach_session(thread_name, str(session_file))

                async def after_agent_info(self, event: events.AgentInfo) -> None:
                    await agent.config.observe_agent_info(session_id, thread_name, event)

                @handles(events.ToolEnd)
                async def tool_ended(self, event: events.ToolEnd) -> None:
                    nonlocal thread_name, successful_tool_observed
                    if event.ok:
                        successful_tool_observed = True
                    thread_name = await agent.sessions.sync_identity(session_id)
                    if event.name == "comms_set_goal" and event.ok is True:
                        current_goal = agent._comms.registry.require(thread_name).goal
                        if current_goal is not None:
                            store = agent._open_goal_store()
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
                                agent._pending_goal_origins[thread_name] = current_goal.id
                    update_turn_activity(ActivityState.THINKING, task[:80])

                @handles(events.StreamSettled)
                async def stream_settled(self, event: events.StreamSettled) -> None:
                    nonlocal compaction_resume_activity, terminal_fence, settled
                    compaction_resume_activity = None
                    terminal_fence = agent.finish_turn_stream(
                        session_id, thread_name, turn_id, turn_claim
                    )
                    settled = True
                    finish_event.set()

                async def consume(self, event: events.AgentEvent) -> None:
                    event = await self.dispatch(event)
                    await agent._emit_event(session_id, event, turn_id=turn_id, route=routing.reply)
                    await publication.dispatch(event)

            publication = TurnEventPublication()
            consumer = TurnEventConsumer()
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
                await consumer.consume(event)
            if terminal_ok is None and goal is not None and goal.active and not direct_interrupt:
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
                            "Backend turn ended without a result; inspect local diagnostics."
                        ),
                    )
            if goal_permit is not None:
                assert goal is not None
                assert self._goal_store is not None
                current_goal = self._comms.registry.require(thread_name).goal
                verified_report = (
                    current_goal is not None
                    and current_goal.id == goal.id
                    and current_goal.reported_turn == turn_id
                )
                if terminal_ok is True and current_goal is not None and current_goal.id == goal.id:
                    witness = f"native-terminal:{turn_id}"
                    if isinstance(current_goal.state, CompletedGoal) and verified_report:
                        witness = f"registry-revision:{current_goal.revision}"
                        self._goal_store.record_verified_completion(goal_permit, witness)
                        goal_attempt_resolved = True
                    elif current_goal.active or isinstance(current_goal.state, PausedGoal):
                        # A successful in-flight turn may finish after owner pause.
                        # Preserve success; the scheduler will not launch while paused.
                        self._goal_store.record_verified_progress(goal_permit, witness)
                        goal_attempt_resolved = True
                if not goal_attempt_resolved:
                    terminal_snapshot = self._comms.registry.snapshot()
                    with suppress(StaleAttempt):
                        self._goal_store.record_failed(
                            goal_permit.reservation,
                            "Goal turn ended without verified terminal progress.",
                            observation=(
                                FailedTurnObservation.from_terminal(
                                    goal_permit.reservation,
                                    owner=thread,
                                    goal=goal,
                                    claim=turn_claim,
                                    turn_id=turn_id,
                                    admission=turn_admission,
                                    current_owner=terminal_snapshot.threads.get(thread_name),
                                    current_admission=terminal_snapshot.admission_generations.get(
                                        thread_name
                                    ),
                                    reason=terminal_failure_reason(terminal_failure),
                                )
                                if terminal_ok is not True
                                else None
                            ),
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
                # unrelated session. Persist only structural facts for headless owners.
                diagnostic_path = record_terminal_failure(
                    self._comms.root,
                    turn_id=turn_id,
                    thread=thread_name,
                    event=terminal_failure,
                    sequences=tuple(origin.seq for origin in origins),
                )
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
                        f"{prefix}: backend turn did not complete. "
                        f"[Open diagnostic]({diagnostic_path.as_uri()})",
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
                    and isinstance(current.state, (ActiveGoal, PausedGoal, CompletedGoal))
                )
                assert self._goal_store is not None
                resolved_origin_permit = originated_attempts.get(originated_id)
                if resolved_origin_permit is not None:
                    if (
                        valid_origin
                        and current is not None
                        and isinstance(current.state, (ActiveGoal, PausedGoal))
                    ):
                        self._goal_store.record_verified_progress(
                            resolved_origin_permit, f"origin-final:{turn_id}"
                        )
                    elif (
                        valid_origin
                        and current is not None
                        and isinstance(current.state, CompletedGoal)
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
                    if generation is not None and generation.lifecycle.allows_resume(True):
                        self._goal_store.retire_goal(
                            originated_id,
                            expected_generation=generation.number,
                            attempt_id=generation.attempt_id,
                        )
                if (
                    not valid_origin
                    and current is not None
                    and current.id == originated_id
                    and isinstance(current.state, (ActiveGoal, CompletedGoal))
                ):
                    self._comms.update_goal(
                        thread_name,
                        BlockedGoalAction(
                            expect=GoalPrecondition(goal_id=originated_id, expected_goal=current),
                            progress="Goal origin turn did not finish successfully.",
                        ),
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
            thread_name = await self.sessions.sync_identity(session_id)
            current_project = self._comms.registry.require(thread_name).worktree
            if current_project != thread.worktree and (
                persistent := self._persistent_backends.get(session_id)
            ):
                await persistent.close_idle()
            await self.inputs.finish_turn_inputs(session_id, backend_inbox)
            if not cancelled and not self.inputs.closing and current_project != thread.worktree:
                self.inputs.pending_turns.setdefault(session_id, []).append(
                    ScheduledTurn(
                        f"Project change completed: tools and context now use {current_project!r}. "
                        "Continue the user's previous request from this directory. "
                        "If the request was only to switch projects, report that you are ready; "
                        "do not invent extra work."
                    )
                )
            await self.settle_turn(
                session_id,
                thread_name,
                turn_id,
                turn_claim,
                stream_settled=settled,
                terminal_fence=terminal_fence,
            )

    def finish_turn_stream(
        self,
        session_id: str,
        thread_name: str,
        turn_id: str,
        claim: TurnLeaseFence,
    ) -> FinishedTurnFence | None:
        """Clear only this turn; waiter release follows committed terminal output."""
        fence = self._comms.finish_turn(thread_name, turn_id, expected=claim)
        if self._active_turns.get(session_id) == turn_id:
            self._active_turns.pop(session_id, None)
        return fence

    async def settle_turn(
        self,
        session_id: str,
        thread_name: str,
        turn_id: str,
        claim: TurnLeaseFence,
        *,
        stream_settled: bool = False,
        terminal_fence: FinishedTurnFence | None = None,
        task: asyncio.Task[Any] | None = None,
    ) -> None:
        """Settle after terminal publication, releasing waiters even if the UI fails.

        Native StreamSettled has already finished and published the stream. Its
        fence is retained until Done and relay output are committed. Manual
        compaction and relay turns take both phases here.
        """
        if task is not None and self._turn_tasks.get(session_id) is task:
            self._turn_tasks.pop(session_id, None)
        if not stream_settled:
            terminal_fence = self.finish_turn_stream(session_id, thread_name, turn_id, claim)
        try:
            if not stream_settled:
                await self._emit_event(session_id, events.TurnSettled(turn_id))
        finally:
            self._comms.release_waits_after_terminal_turn(terminal_fence)

    def _started_event(self, thread_name: str, turn_id: str) -> StartedTranscriptUpdate:
        """Project one owner-authored turn without inventing presentation timestamps."""
        thread = self._comms.registry.require(thread_name)
        active = thread.active_turn
        activity = self._comms.activity_of(thread.name)
        return StartedTranscriptUpdate(
            turn_id=turn_id,
            started_at=active.started_at if active is not None and active.id == turn_id else None,
            activity=activity.state.value,
            activity_detail=activity.detail,
        )

    async def replay_turn_state(self, session_id: str, client: Any = None) -> None:
        thread_name = self._require_session(session_id)
        active = self._comms.registry.require(thread_name).active_turn
        event = (
            self._started_event(thread_name, active.id)
            if active is not None
            else events.NoActiveTurn()
        )
        await self._emit_event(session_id, event, client=client)

    async def shutdown(self) -> None:
        """Stop drains and mark threads owned by this ACP connection offline."""
        await self.inputs.stop_wakes()
        await self.sessions.close_proxies()
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
        await self.inputs.close()
        await self.sessions.release_owned()

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

    async def _emit_event(
        self,
        session_id: str,
        event: events.AgentEvent | TranscriptUpdate | dict[str, Any],
        client: Any = None,
        *,
        turn_id: str | None = None,
        route: MessageRoute | None = None,
    ) -> None:
        """Publish typed backend events or existing wire/transcript messages."""
        client = client or self._runtime
        if isinstance(event, events.AgentEvent):
            await AcpEventConsumer(self, session_id, client, turn_id, route).dispatch(event)
            return
        update = TranscriptUpdate.from_legacy(event) if isinstance(event, dict) else event
        await update.publish(session_id, client)

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
    """A stdio connection attaches to a separately owned executor."""

    session_lifecycle_class = AttachedSessionLifecycle


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
    # The stdio ACP client only attaches to a separately owned worker. An
    # explicit private launch must be checked before it can create a wire or
    # request an owner; the worker independently repeats the same preflight.
    from .private_nk_entrypoint import private_nk_from_environment

    private_nk = private_nk_from_environment()
    comms = wire(private_nk.validated_root) if private_nk is not None else wire()
    if private_nk is not None:
        comms.pin_private_nk_launch(
            private_nk.validated_root, private_nk.wire_root_id, private_nk.native_package
        )

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
