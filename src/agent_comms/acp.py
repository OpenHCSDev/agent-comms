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
import sys
import time
from contextlib import AsyncExitStack
from pathlib import Path
from functools import partial
from typing import TYPE_CHECKING, Any

from acp import RequestError, run_agent
from acp.schema import (
    AgentMessageChunk,
    InitializeResponse,
    LoadSessionResponse,
    NewSessionResponse,
    PromptResponse,
    SetSessionConfigOptionResponse,
    TextContentBlock,
)

from agent_comms.coordination_errors import (
    IdentityConflict,
    PublicationActivationBlocked,
    StaleFence,
)
from agent_comms.coordinator import Coordination
from agent_comms.native_source_cursor import NativeSourceCursor

from . import agent_events as events
from .acp_extension import (
    CompactRequest,
    TextRouteUpdate,
    encode_updates,
)
from .agent_event_updates import AcpEventConsumer
from .bus_publication import stable_thread_lookup
from .cohort_foreground import _accept_visible_deliveries
from .comms import Comms, wire
from .coordination_cohort import next_sealed_assignment
from .field_codec import FieldCodec
from .cursor_publication import CursorPublication
from .input_drain import InputDrain
from .input_effects import InputEffects
from .message_bus import MessageBus
from .routing import MessageRoute
from .runtime import (
    UNBOUND_CONTROLLER,
    RuntimeProxy,
    RuntimeServer,
    socket_path,
)
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
        use_unstable_protocol: bool = False,
        auto_wake: bool = True,
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
        self._comms = comms
        self.use_unstable_protocol = use_unstable_protocol
        # Enabled for verified native owners by default. Explicit construction
        # may disable it; model/tool content cannot change this owner policy.
        self._runtime = RuntimeServer(self)
        from .working_memory_policy import AnnotationPolicy
        from .working_memory_worker import AnnotationWorker

        self.annotations = AnnotationWorker(comms, self._runtime, self, AnnotationPolicy.configured())
        self.cursors = CursorPublication(comms, self._runtime, private_nk_wire_root_id)
        self.turns = TurnRunner(
            comms,
            self._runtime,
            self,
            agent_bin=agent_bin,
            agent_args=agent_args,
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
        from .acp_extension import decode_request
        from .acp_request_consumer import AcpRequestConsumer

        try:
            request = decode_request(kwargs.get("_meta") or kwargs.get("field_meta"), **kwargs)
        except (ValueError, TypeError) as error:
            raise RequestError.invalid_params({"reason": str(error)}) from error
        # Literal /compact is the external ACP text command; ours uses a record.
        text = self._prompt_text(prompt).strip()
        if re.match(r"^/compact(?:\s|$)", text):
            self._require_compaction_text(prompt)
            request = CompactRequest(text[len("/compact") :].strip())
        return await AcpRequestConsumer(self, session_id, prompt).run(request)

    async def _compact_request(self, session_id: str, instructions: str | None) -> PromptResponse:
        from . import manual_compaction_bridge
        from .compaction_result import CompactionResult

        # Idle owner bridge alone owns the lock and the one-POST budget.
        if session_id in self.sessions.proxies:
            result = FieldCodec.decode(
                CompactionResult,
                await self.sessions.proxies[session_id].request(
                    "compact", instructions=instructions
                ),
            )
        else:
            result = await manual_compaction_bridge.compact_context(
                self.turns, session_id, instructions
            )
        return result.prompt_response()

    async def _selected_write_request(self, session_id: str, request) -> PromptResponse:
        from .selected_write_plan import SelectedWritePlans

        owner = self.sessions.require(session_id)
        root_id = self._private_nk_marker()
        controller = self._runtime.controller.get()
        if controller is None or (
            controller is UNBOUND_CONTROLLER and self.sessions.client is None
        ):
            raise RequestError.invalid_params(
                {"reason": "Selected write requires attached ACP controller"}
            )
        receipt = SelectedWritePlans(self._comms, root_id).submit(
            owner_name=owner,
            source_seq=request.source_seq,
            source_message_id=request.source_message_id,
            resource=request.resource,
            contents=request.contents,
        )
        attached = self.sessions.client if controller is UNBOUND_CONTROLLER else controller
        self._selected_write_controllers[(owner, request.source_seq)] = (
            receipt.operation_id,
            attached,
        )
        return PromptResponse(stop_reason="end_turn", field_meta=encode_updates(receipt))

    async def _prompt_request(self, session_id: str, prompt: list[Any], request) -> PromptResponse:
        display_text = request.user_text or self._prompt_text(prompt)
        try:
            images = self._prompt_images(prompt)
        except ValueError as error:
            raise RequestError.invalid_params({"reason": str(error)}) from error
        if images and self._prompt_text(prompt).lstrip().startswith(("@", "#", RELAY_PREFIX)):
            raise RequestError.invalid_params(
                {"reason": "Send images to an agent thread, not as a coordination relay."}
            )
        text = self._prompt_text(prompt)
        if (
            await self.turns.active_backend_inbox(session_id) is not None
            and not text.lstrip().startswith(("@", "#", RELAY_PREFIX))
        ):
            return await self.inputs.accept_followup(
                session_id,
                text=text,
                display_text=display_text,
                request=request,
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
                    session_id, prompt, display_text=display_text, input_id=request.input_id,
                    origin=request.origin,
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
    def _require_compaction_text(prompt: list[Any]) -> None:
        if len(prompt) != 1:
            raise RequestError.invalid_params({"reason": "/compact requires one text block."})
        block = prompt[0]
        kind = block.get("type") if isinstance(block, dict) else getattr(block, "type", None)
        if kind != "text":
            raise RequestError.invalid_params({"reason": "/compact requires text only."})

    async def emit_session_identity(self, session_id: str, name: str, client: Any = None) -> None:
        """Let a subscriber identify its owner before potentially long replay."""
        await (client or self._runtime).session_update(
            session_id=session_id,
            update=AgentMessageChunk(
                session_update="agent_message_chunk",
                content=TextContentBlock(type="text", text=""),
                field_meta=await self.sessions.metadata(name, session_id=session_id),
            ),
        )

    def _debug_log(self, message: str) -> None:
        debug_path = os.environ.get("AGENT_COMMS_DEBUG_LOG")
        if debug_path:
            with open(debug_path, "a") as debug_log:
                debug_log.write(f"[{time.time():.3f}] {message}\n")

    async def cancel(self, session_id: str, **kwargs: Any) -> None:
        return await self.turns.cancel(session_id, **kwargs)

    async def close_session(self, session_id: str, **kwargs: Any):
        return await self.sessions.close_session(session_id)

    async def set_config_option(
        self, config_id: str, session_id: str, value: str | bool, **kwargs: Any
    ) -> SetSessionConfigOptionResponse:
        return await self.sessions.config.set_option(config_id, session_id, value)

    async def authenticate(self, method_id: str, **kwargs: Any) -> None:
        raise RequestError.auth_required({"reason": "agent-comms requires no authentication"})

    # ─── Helpers ─────────────────────────────────────────────────────────────

    def _private_nk_marker(self) -> str:
        """Require the configured, certified root before any selected request."""
        from .private_bus_checkpoint import verify_private_bus_checkpoint_unlocked

        if self._private_nk_wire_root_id is None or self._private_nk_native_package is None:
            raise PublicationActivationBlocked(
                "private N/K ACP session requires explicit matching root and package"
            )
        with self._comms.bus.log.locked():
            marker = self._comms.bus.log._private_marker_unlocked()
            if marker.root_id != self._private_nk_wire_root_id:
                raise PublicationActivationBlocked(
                    "private N/K ACP root does not match configuration"
                )
            verify_private_bus_checkpoint_unlocked(self._comms.bus.log, marker)
            return marker.root_id

    async def _drain_private_nk(self, session_id: str, wire_root_id: str) -> int:
        """Run a selected private wake for this ACP session.

        This explicitly configured path reuses the reviewed one-shot native
        reservation/send boundary. No schema/participant is installed here;
        both must already belong to the same private root. A failing or
        uncertain native turn propagates and cannot be replayed by a drain.
        """
        from .coordinated_runtime import SelectedExecution
        from .selected_write_authority import AcpSelectedWriteAuthority
        from .selected_write_plan import SelectedWritePlans

        package = self._private_nk_native_package
        if package is None or self._private_nk_wire_root_id != wire_root_id:
            raise PublicationActivationBlocked(
                "private N/K ACP requires an explicit matching root and native package"
            )
        thread_name = await self.sessions.sync_identity(session_id)
        snapshot = await Coordination.run_worker(self._comms.registry.snapshot)
        owner = snapshot.require(thread_name)
        if (
            self.inputs.background_wakes_disabled
            or snapshot.status(thread_name).stopped
            or session_id in self.turns.turn_tasks
            or owner.turn_state.busy
            or session_id in self.inputs.backend_inboxes
        ):
            # Admission changes still invalidate the client while this owner
            # cannot run a wake. An eligible drain observes once after its
            # acceptance/selection, instead of verifying history before it too.
            await self.cursors.publish(session_id, thread_name)
            return 0
        if owner.pid != os.getpid():
            raise IdentityConflict("private N/K ACP recipient is not this process owner")
        if owner.turn_state.busy:
            raise StaleFence("private N/K ACP owner is busy")
        bus = MessageBus(
            self._comms.root / "bus.jsonl", self._comms.registry, private_response_writes=True
        )
        def read_admission():
            with bus.log.locked():
                return bus.log._private_marker_unlocked().admission_after_seq
        admission_after_seq = await Coordination.run_worker(read_admission)
        store_path = self._comms.root / "coordination.sqlite3"
        # One execution owns this wake's package acquisition: acceptance of new
        # originals and the selected launch share it instead of verifying twice.
        execution = SelectedExecution(
            root=self._comms.root,
            wire_root_id=wire_root_id,
            owner_name=thread_name,
            native_package=package,
            write_authority=AcpSelectedWriteAuthority(
                self, session_id, SelectedWritePlans(self._comms, wire_root_id)
            ),
            **(
                {"selected_tool_intent": self._private_selected_tool_intent}
                if self._private_selected_tool_intent is not None
                else {}
            ),
        )
        await _accept_visible_deliveries(
            bus, wire_root_id, store_path, stable_thread_lookup(owner.created_at), 0,
            owner_name=owner.name, acquire_package=execution.validate,
        )
        def select(store):
            participant = store.participants.get(stable_thread_lookup(owner.created_at))
            candidate = next_sealed_assignment(
                store, participant.lookup, owner.name, after_seq=admission_after_seq,
            )
            return candidate is not None and participant.pointer.execution_id is None
        runnable = await Coordination.run_async(store_path, select)
        result = None
        if runnable:
            result = await self.turns.run_selected(session_id, execution)
        if result is None:
            # N (or absent-audience) rows prove coverage, not an injected
            # input. Extend only an existing current generation or an all-N prefix;
            # old-generation Pi evidence cannot initialize this cursor on reconnect.
            await self.cursors.publish(
                session_id, thread_name, read_cursor=NativeSourceCursor.refresh_async
            )
        else:
            # A disconnected client must not turn a settled claim into an
            # apparent model failure. Reconnect reads the same durable row.
            await self.cursors.publish(
                session_id, thread_name, selected_status=result.cursor_status
            )
        return int(result is not None)

    async def shutdown(self) -> None:
        """Stop drains and mark threads owned by this ACP connection offline."""
        async with AsyncExitStack() as retirement:
            retirement.push_async_callback(self.sessions.release_owned)
            retirement.push_async_callback(self.turns.close)
            retirement.push_async_callback(self.sessions.close_proxies)
            retirement.push_async_callback(self.inputs.close)
            retirement.push_async_callback(self.annotations.close)

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
                field_meta=encode_updates(TextRouteUpdate(route)),
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
    from .active_route import resolve_comms_route

    with resolve_comms_route().admit_client():
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
            agent = CommsClient(
                comms,
                runtime_enabled=True,
                use_unstable_protocol=True,
                private_nk_native_package=private_nk.native_package if private_nk else None,
                private_nk_wire_root_id=private_nk.wire_root_id if private_nk else None,
            )

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
                await run_agent(agent, use_unstable_protocol=agent.use_unstable_protocol,
                                **conn_kwargs)  # type: ignore[arg-type]
            finally:
                await agent.shutdown()

        asyncio.run(run())
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
