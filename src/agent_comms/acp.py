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

An explicit ``!agent`` prefix selects the coding turn mode.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import sqlite3
import sys
import time
from dataclasses import asdict
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from acp import RequestError, run_agent
from acp.schema import (
    AgentMessageChunk,
    InitializeResponse,
    LoadSessionResponse,
    NewSessionResponse,
    PromptResponse,
    SessionInfoUpdate,
    SetSessionConfigOptionResponse,
    TextContentBlock,
)

from . import agent_events as events
from . import backend, manual_compaction_bridge
from .agent_event_updates import AcpEventConsumer
from .bus_publication import stable_thread_lookup
from .cohort_foreground import _accept_visible_initials
from .comms import Comms, wire
from .coordinated_runtime import SelectedExecution
from .coordination import CoordinationError, WakeAssignment
from .coordination_cohort import next_sealed_assignment
from .coordination_store import (
    IdentityConflict,
    MutationStore,
    PublicationActivationBlocked,
    StaleFence,
)
from .field_codec import FieldCodec
from .input_drain import InputDrain
from .input_effects import InputEffects
from .message_bus import MessageBus
from .native_source_cursor import advance_current_native_cursor, read_current_native_cursor
from .routing import MessageRoute
from .runtime import (
    UNBOUND_CONTROLLER,
    RuntimeProxy,
    RuntimeServer,
    SocketClient,
    socket_path,
)
from .selected_write_plan import PlannedWrite, SelectedWritePlans
from .session_effects import SessionEffects
from .session_lifecycle import AttachedSessionLifecycle, SessionLifecycle
from .threads import Thread
from .transcript_updates import TranscriptUpdate
from .turn_effects import TurnEffects
from .turn_runner import TurnRunner

if TYPE_CHECKING:
    from .selected_tool_broker import SelectedToolIntent

from .turn_runner import RELAY_PREFIX


class CommsAgent(SessionEffects, InputEffects, TurnEffects):
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
        self._runtime = RuntimeServer(self)
        self.turns = TurnRunner(
            comms,
            self._runtime,
            self,
            agent_bin=agent_bin,
            agent_args=agent_args,
            adaptive_compaction_enabled=adaptive_compaction_enabled,
            adaptive_summary_strategy=adaptive_summary_strategy,
            reply_window=reply_window,
            no_reply_window=no_reply_window,
            reply_quiet=reply_quiet,
        )
        self.sessions = self.session_lifecycle_class(
            comms, self.turns.agent_bin, self.turns.agent_args, self._runtime, runtime_enabled, self
        )
        self.inputs = InputDrain(comms, self.sessions, self._runtime, self, auto_wake)
        self.turns.bind(self.sessions, self.inputs)
        # A preplanned write is valid only while its original ACP controller
        # remains attached. Never restore this binding after a process crash.
        self._selected_write_controllers: dict[tuple[str, int], tuple[str, object]] = {}

    def _create_runtime_proxy(self, thread: Thread, session_id: str) -> RuntimeProxy:
        return RuntimeProxy(self, session_id, socket_path(self._comms.root, thread.pid))

    # Private attachment ABI consumed by runtime/compaction. State exists only
    # on its component; in-process consumers use that owner directly.

    def on_connect(self, client: Any) -> None:
        """Called by AgentSideConnection with the client-facing connection."""
        self.sessions.client = client

    # ─── ACP methods ─────────────────────────────────────────────────────────

    async def publish_pending_compaction(self, session_id: str, thread_name: str) -> int:
        from .compaction_publication import publish_pending_local

        return await publish_pending_local(self, session_id, thread_name)

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

    async def prompt(self, session_id: str, prompt: list[Any], **kwargs: Any) -> PromptResponse:
        instructions = self._manual_compaction_instructions(prompt, kwargs)
        if instructions is not None:
            # Idle owner bridge alone owns the lock and the one-POST budget.
            if session_id in self.sessions.proxies:
                result = await self.sessions.proxies[session_id].request(
                    "compact", instructions=instructions
                )
            else:
                result = await manual_compaction_bridge.compact_context(
                    self.turns, session_id, instructions
                )
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
            owner = self.sessions.require(session_id)
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
                else backend.rpc_args_for(self.turns.agent_bin, self.turns.agent_args) is not None
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
            session_id in self.turns.active_turns
            and backend.rpc_args_for(self.turns.agent_bin, self.turns.agent_args) is not None
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
        async with self.turns.turn_locks.setdefault(session_id, asyncio.Lock()):
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
                return await self.turns.prompt_owned(
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

    async def emit_session_identity(self, session_id: str, name: str, client: Any = None) -> None:
        """Let a subscriber identify its owner before potentially long replay."""
        await (client or self._runtime).session_update(
            session_id=session_id,
            update=AgentMessageChunk(
                session_update="agent_message_chunk",
                content=TextContentBlock(type="text", text=""),
                field_meta=self.sessions.metadata(name, session_id=session_id),
            ),
        )

    def _debug_log(self, message: str) -> None:
        debug_path = os.environ.get("AGENT_COMMS_DEBUG_LOG")
        if debug_path:
            with open(debug_path, "a") as debug_log:
                debug_log.write(f"[{time.time():.3f}] {message}\n")

    async def cancel(self, session_id: str, **kwargs: Any) -> None:
        return await self.turns.cancel(session_id, **kwargs)

    async def set_config_option(
        self, config_id: str, session_id: str, value: str | bool, **kwargs: Any
    ) -> SetSessionConfigOptionResponse:
        return await self.sessions.config.set_option(config_id, session_id, value)

    async def authenticate(self, method_id: str, **kwargs: Any) -> None:
        raise RequestError.auth_required({"reason": "agent-comms requires no authentication"})

    # ─── Helpers ─────────────────────────────────────────────────────────────

    def _private_cursor_scope(self, thread_name: str, session_id: str) -> dict[str, Any] | None:
        root_id = self._private_nk_wire_root_id
        if root_id is None:
            return None
        try:
            owner, admission_generation = self._comms.registry.live_owner_with_admission(
                thread_name
            )
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
            "ownerEpoch": admission_generation,
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
                **FieldCodec.encode(cursor),
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
        with self._comms.bus.log.locked():
            if marker_path.is_symlink():
                raise IdentityConflict("ACP bus marker is redirected")
            if not marker_path.exists():
                return None
            metadata = self._comms.bus.log.read_metadata_unlocked(required=True)
            if metadata.private:
                return self._comms.bus.log._private_marker_unlocked().root_id
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
        thread_name = await self.sessions.sync_identity(session_id)
        # Registry admission may change without session/new or session/load.
        # Publish the observed status even when stopped, busy, or no-wake;
        # callbacks may only invalidate a prior client binding, not replace it.
        await self._publish_private_cursor(session_id, thread_name)
        if not self.inputs.auto_wake or not self.sessions.runtime_enabled:
            return 0  # Explicitly disabled: no legacy path or ACK fallback.
        if self._comms.registry.status(thread_name).stopped:
            return 0
        if (
            session_id in self.turns.active_turns
            or session_id in self.turns.turn_tasks
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
        with bus.log.locked():
            admission_after_seq = bus.log._private_marker_unlocked().admission_after_seq
        with MutationStore(str(self._comms.root / "coordination.sqlite3")) as store:
            _accept_visible_initials(
                bus,
                wire_root_id,
                store,
                stable_thread_lookup(owner.created_at),
                0,
                owner_name=owner.name,
                native_package=package,
            )
            participant = store.participant(stable_thread_lookup(owner.created_at))
            candidate = next_sealed_assignment(
                store, participant.lookup, owner.name, after_seq=admission_after_seq
            )
            runnable = candidate is not None and participant.pointer.execution_id is None
        plans = SelectedWritePlans(self._comms, wire_root_id)

        def check_plan_controller(
            assignment: WakeAssignment, owner: Thread, operation_id: str
        ) -> None:
            bound = self._selected_write_controllers.get((owner.name, assignment.wire_seq))
            if bound is None or bound[0] != operation_id:
                raise IdentityConflict("Selected write original controller is no longer bound")
            controller = bound[1]
            if isinstance(controller, SocketClient):
                if not self._runtime.is_controller(session_id, controller):
                    raise IdentityConflict("Selected write controller disconnected")
            elif controller is not self.sessions.client or controller is None:
                raise IdentityConflict("Selected write ACP controller changed")

        def load_plan(
            assignment: WakeAssignment, owner: Thread, admission_generation: int
        ) -> PlannedWrite | None:
            plan = plans.load(assignment, owner, admission_generation)
            if plan is not None:
                check_plan_controller(assignment, owner, plan.operation_id)
            return plan

        def applied_plan(assignment: WakeAssignment, owner: Thread, operation_id: str) -> None:
            plans.applied(assignment, owner, operation_id)
            self._selected_write_controllers.pop((owner.name, assignment.wire_seq), None)

        result = None
        if runnable:
            result = await SelectedExecution(
                root=self._comms.root,
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
            ).run()
        if result is None:
            # N (or absent-audience) rows prove coverage, not an injected
            # input. Extend only an existing current epoch or an all-N prefix;
            # old-epoch Pi evidence cannot initialize this cursor on reconnect.
            try:
                with MutationStore(str(self._comms.root / "coordination.sqlite3")) as store:
                    person = store.participant(stable_thread_lookup(owner.created_at))
                    admission_generation = self._comms.registry.snapshot().admission_generations[
                        thread_name
                    ]
                    cursor = advance_current_native_cursor(
                        bus,
                        store,
                        wire_root_id=wire_root_id,
                        owner=owner,
                        owner_admission_generation=admission_generation,
                        owner_generation=person.participant_generation,
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

    async def shutdown(self) -> None:
        """Stop drains and mark threads owned by this ACP connection offline."""
        await self.inputs.stop_wakes()
        await self.sessions.close_proxies()
        await self.turns.close()
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
        event: events.AgentEvent | TranscriptUpdate,
        client: Any = None,
        *,
        turn_id: str | None = None,
        route: MessageRoute | None = None,
    ) -> None:
        """Publish typed live or saved transcript events."""
        client = client or self._runtime
        if isinstance(event, events.AgentEvent):
            await AcpEventConsumer(self, session_id, client, turn_id, route).dispatch(event)
            return
        await event.publish(session_id, client)

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
        comms.owners.pin_private_nk_launch(
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
