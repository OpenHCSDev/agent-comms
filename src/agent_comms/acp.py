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
from functools import partial
from typing import TYPE_CHECKING

from acp import RequestError
from acp.agent.connection import AgentSideConnection
from acp.connection import StreamEvent
from acp.core import DEFAULT_STDIO_BUFFER_LIMIT_BYTES
from acp.stdio import stdio_streams
from acp.schema import (
    ClientCapabilities,
    Implementation,
    AgentMessageChunk,
    InitializeResponse,
    LoadSessionResponse,
    NewSessionResponse,
    PromptResponse,
    SetSessionConfigOptionResponse,
    TextContentBlock,
)
from pydantic import JsonValue

from agent_comms.coordination_errors import IdentityConflict
from agent_comms.coordinator import Coordination

from .acp_extension import (
    CompactRequest,
    encode_updates,
)
from .acp_failure import NativeTurnError
from .channel_targets import NamedTarget
from .cohort_foreground import _accept_visible_deliveries
from .comms import Comms, wire
from .controller import AcpController, Controller, SessionUpdateSink, UNBOUND_CONTROLLER
from .coordination_cohort import next_sealed_assignment
from .coordination_database import CoordinationStore
from .cursor_owner import CursorOwner
from .cursor_publication import CursorPublication
from .field_codec import FieldCodec, MalformedRecord
from .image_inputs import prompt_images
from .input_drain import InputDrain
from .input_effects import InputEffects
from .launch_root import LaunchRoot
from .message_bus import MessageBus
from .publisher import ResponseWrites
from .queued_input import DeliveredInput
from .runtime import (
    RuntimeProxy,
    RuntimeServer,
    socket_path,
)
from .schedule_rules import SelectedWakeCheck
from .session_effects import SessionEffects
from .session_lifecycle import AttachedSessionLifecycle, SessionLifecycle, SessionMcpServer
from .threads import Thread
from .turn_effects import PromptBlock, TurnEffects
from .turn_runner import TurnRunner
from .turn_terminal import EndedWithoutResult, TurnInterruption

if TYPE_CHECKING:
    from .selected_tool_broker import SelectedToolIntent

from .native_arguments import ConfiguredBinary, InheritedArguments, LaunchArguments, LaunchBinary
from .turn_runner import RELAY_PREFIX


class CommsAgent(SessionEffects, InputEffects, TurnEffects):
    """ACP agent bound to one Comms wire.

    Session -> thread. Coding prompts run the configured backend; targeted
    prompts publish to the shared wire. Replies belong to the thread's bus view.
    """

    session_lifecycle_class = SessionLifecycle

    def __init__(
        self,
        comms: Comms,
        agent_bin: LaunchBinary = ConfiguredBinary(),
        agent_args: LaunchArguments = InheritedArguments(),
        runtime_enabled: bool = False,
        use_unstable_protocol: bool = False,
        auto_wake: bool = True,
        private_selected_tool_intent: SelectedToolIntent | None = None,
        owner_stop: TurnInterruption = EndedWithoutResult(),
    ):
        self._comms = comms
        if private_selected_tool_intent is not None:
            from .selected_tool_broker import SelectedToolIntent

            if type(private_selected_tool_intent) is not SelectedToolIntent:
                raise TypeError("private selected tool needs a nominal owner intent")
            self._comms.owners.require_private_launch()
        self._private_selected_tool_intent = private_selected_tool_intent
        self.use_unstable_protocol = use_unstable_protocol
        # Enabled for verified native owners by default. Explicit construction
        # may disable it; model/tool content cannot change this owner policy.
        self._runtime = RuntimeServer(self)
        from .working_memory_policy import AnnotationPolicy
        from .working_memory_worker import AnnotationWorker

        self.annotations = AnnotationWorker(comms, self._runtime, self, AnnotationPolicy.configured())
        self.cursors = CursorPublication(comms, self._runtime)
        self.turns = TurnRunner(
            comms,
            self._runtime,
            self,
            agent_bin=agent_bin,
            agent_args=agent_args,
            owner_stop=owner_stop,
        )
        self.sessions = self.session_lifecycle_class(
            comms, self.turns.agent_bin, self.turns.agent_args, self._runtime, runtime_enabled, self
        )
        self.inputs = InputDrain(comms, self.sessions, self._runtime, self, auto_wake)
        self.turns.bind(self.sessions, self.inputs)
        # A preplanned write is valid only while its original ACP controller
        # remains attached. Never restore this binding after a process crash.
        self._selected_write_controllers: dict[tuple[str, int], tuple[str, Controller]] = {}

    def _create_runtime_proxy(self, thread: Thread, session_id: str) -> RuntimeProxy:
        return RuntimeProxy(
            self, session_id, socket_path(self._comms.root, thread.require_process().pid),
        )

    # Private attachment ABI consumed by runtime/compaction. State exists only
    # on its component; in-process consumers use that owner directly.

    def on_connect(self, client: AcpController) -> None:
        """Called by AgentSideConnection with the client-facing connection."""
        self.sessions.client = client

    # ─── ACP methods ─────────────────────────────────────────────────────────

    async def initialize(
        self,
        protocol_version: int,
        client_capabilities: ClientCapabilities | None = None,
        client_info: Implementation | None = None,
    ) -> InitializeResponse:
        return await self.sessions.initialize(protocol_version, client_capabilities, client_info)

    async def new_session(
        self, cwd: str, mcp_servers: list[SessionMcpServer] | None = None, **kwargs: JsonValue
    ) -> NewSessionResponse:
        return await self.sessions.new_session(cwd, mcp_servers, **kwargs)

    async def load_session(
        self,
        cwd: str,
        session_id: str,
        mcp_servers: list[SessionMcpServer] | None = None,
        **kwargs: JsonValue,
    ) -> LoadSessionResponse:
        return await self.sessions.load_session(cwd, session_id, mcp_servers, **kwargs)

    async def prompt(self, session_id: str, prompt: list[PromptBlock], **kwargs: JsonValue) -> PromptResponse:
        from .acp_extension import decode_request
        from .acp_request_consumer import AcpRequestConsumer

        try:
            request = decode_request(kwargs.get("_meta") or kwargs.get("field_meta"), **kwargs)
        except MalformedRecord as error:
            raise RequestError.invalid_params({"reason": str(error)}) from error
        # Literal /compact is the external ACP text command; ours uses a record.
        text = self._prompt_text(prompt).strip()
        if re.match(r"^/compact(?:\s|$)", text):
            self._require_compaction_text(prompt)
            request = CompactRequest(text[len("/compact") :].strip())
        try:
            return await AcpRequestConsumer(self, session_id, prompt).run(request)
        except NativeTurnError as error:
            raise RequestError.internal_error(error.request.error_data()) from error.__cause__

    async def _compact_request(self, session_id: str, instructions: str | None) -> PromptResponse:
        from . import manual_compaction_bridge
        from .agent_backend import CompactionOutcome

        # The owner process's bridge alone owns the turn lock.
        if session_id in self.sessions.proxies:
            outcome = FieldCodec.decode(
                CompactionOutcome,
                await self.sessions.proxies[session_id].request(
                    "compact", instructions=instructions
                ),
            )
        else:
            outcome = await manual_compaction_bridge.compact_context(
                self.turns, session_id, instructions
            )
        return manual_compaction_bridge.CompactionReply().reply(outcome)

    async def _selected_write_request(self, session_id: str, request) -> PromptResponse:
        from .selected_write_plan import SelectedWritePlans

        owner = self.sessions.require(session_id)
        controller = self._runtime.controller.get()
        attached = self.sessions.client if controller is UNBOUND_CONTROLLER else controller
        if attached is None:
            raise RequestError.invalid_params(
                {"reason": "Selected write requires attached ACP controller"}
            )
        scope = await Coordination.run_worker(partial(self.cursors.scope, owner, session_id))
        plans = SelectedWritePlans(self._comms, scope.require_live().wire_root_id)
        acknowledgement = await Coordination.run_worker(partial(
            plans.submit,
            owner_name=owner,
            source_seq=request.source_seq,
            source_message_id=request.source_message_id,
            resource=request.resource,
            contents=request.contents,
        ))
        self._selected_write_controllers[(owner, request.source_seq)] = (
            acknowledgement.operation_id,
            attached,
        )
        return PromptResponse(stop_reason="end_turn", field_meta=encode_updates(acknowledgement))

    async def _prompt_request(self, session_id: str, prompt: list[PromptBlock], request) -> PromptResponse:
        display_text = request.user_text or self._prompt_text(prompt)
        try:
            images = prompt_images(prompt)
        except ValueError as error:
            raise RequestError.invalid_params({"reason": str(error)}) from error
        if images and self._prompt_text(prompt).lstrip().startswith(
                (*NamedTarget.sigils(), RELAY_PREFIX)):
            raise RequestError.invalid_params(
                {"reason": "Send images to an agent thread, not as a coordination relay."}
            )
        text = self._prompt_text(prompt)
        if (
            (await self.turns.active_backend_turn(session_id)).is_open
            and not text.lstrip().startswith((*NamedTarget.sigils(), RELAY_PREFIX))
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
    def _require_compaction_text(prompt: list[PromptBlock]) -> None:
        if len(prompt) != 1:
            raise RequestError.invalid_params({"reason": "/compact requires one text block."})
        block = prompt[0]
        kind = block.get("type") if isinstance(block, dict) else getattr(block, "type", None)
        if kind != "text":
            raise RequestError.invalid_params({"reason": "/compact requires text only."})

    async def emit_session_identity(
        self, session_id: str, name: str, client: SessionUpdateSink | AgentSideConnection,
    ) -> None:
        """Let a subscriber identify its owner before potentially long replay."""
        await client.session_update(
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

    async def cancel(self, session_id: str, **kwargs: JsonValue) -> None:
        return await self.turns.cancel(session_id, **kwargs)

    async def close_session(self, session_id: str, **kwargs: JsonValue):
        return await self.sessions.close_session(session_id)

    async def set_config_option(
        self, config_id: str, session_id: str, value: str | bool, **kwargs: JsonValue
    ) -> SetSessionConfigOptionResponse:
        return await self.sessions.config.set_option(config_id, session_id, value)

    async def authenticate(self, method_id: str, **kwargs: JsonValue) -> None:
        raise RequestError.auth_required({"reason": "agent-comms requires no authentication"})

    # ─── Helpers ─────────────────────────────────────────────────────────────

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

        launch = self._comms.owners.require_private_launch()
        package = launch.native_package
        thread_name = await self.sessions.sync_identity(session_id)
        snapshot = await Coordination.run_worker(self._comms.registry.snapshot)
        owner = snapshot.require(thread_name)
        if self.inputs.background_wakes_disabled or snapshot.status(thread_name).stopped:
            # Input permission changes still invalidate the client while this owner
            # cannot run a wake. An eligible drain observes once after its
            # acceptance/selection, instead of verifying history before it too.
            await self.cursors.publish(session_id, thread_name)
            return 0
        if not owner.process_identity.runs_here():
            raise IdentityConflict("private N/K ACP recipient is not this process owner")
        lookup = owner.incarnation.require_recorded().lookup
        bus = MessageBus(self._comms.root, self._comms.registry, ResponseWrites)
        delivery = self.cursors.delivery(session_id)
        async with delivery.reading() as source_reads:
            state = await CursorOwner.read_async(
                bus, wire_root_id=wire_root_id, owner_name=thread_name, source_reads=source_reads,
            )
        after_seq = state.covered_seq
        store_path = CoordinationStore.path_in(self._comms.root)
        # One execution owns this wake's package acquisition: acceptance of new
        # originals and the selected launch share it instead of verifying twice.
        execution = SelectedExecution(
            root=self._comms.root,
            wire_root_id=wire_root_id,
            owner_name=thread_name,
            native_package=package,
            cursor_delivery=delivery,
            write_authority=AcpSelectedWriteAuthority(
                self, session_id, SelectedWritePlans(self._comms, wire_root_id)
            ),
            selected_tool_intent=self._private_selected_tool_intent,
        )
        with execution.cursor_delivery.measurements.operation("accept_deliveries"):
            await _accept_visible_deliveries(
                bus, wire_root_id, store_path, lookup, after_seq,
                owner_name=owner.name, acquire_package=execution.validate,
            )
        if self.inputs.owns_execution(session_id, owner):
            # A running turn takes its owner's pending messages as inputs;
            # whatever it cannot take wakes this owner once the turn ends.
            await DeliveredInput.deliver_pending(
                self.inputs, session_id, owner, bus, wire_root_id, after_seq
            )
            await self.cursors.publish(session_id, thread_name)
            return 0
        def select(store):
            with store.session.read():
                participant = store.participants.get(lookup)
                candidate = next_sealed_assignment(
                    store, participant.lookup, after_seq=after_seq,
                )
                if candidate is None:
                    return False
                # An older turn is still running: refuse idle handling and let
                # the existing drain diagnostic explain the refusal; never retire
                # or replay that turn here.
                participant.pointer.require_idle()
                return True
        if not await Coordination.run_read_async(store_path, select):
            await self.cursors.publish(session_id, thread_name, advance=True)
            return 0
        # The accepted channel row stays in its durable assignment ledger.
        # During cutover, leave it unselected; the maintenance witness wakes
        # observation again on READY, without replaying this bus input.
        if (await Coordination.run_worker(self._comms.owners.maintenance.read)).turn_pending:
            return 0
        SelectedWakeCheck(
            session_id=session_id, inputs=self.inputs, execution=execution,
            thread=thread_name, owner=snapshot.owner_identity(thread_name),
        ).schedule()
        return 1

    async def shutdown(self) -> None:
        """Stop drains and mark threads owned by this ACP connection offline."""
        async with AsyncExitStack() as retirement:
            # CursorPublication owns the cross-observation evidence lifetime;
            # close it after its session/input producers have drained.
            retirement.callback(self.cursors.close)
            retirement.push_async_callback(self.sessions.release_owned)
            retirement.callback(self.inputs.end_sessions)
            retirement.push_async_callback(self.turns.close)
            retirement.push_async_callback(self.sessions.close_proxies)
            retirement.push_async_callback(self.inputs.close)
            retirement.push_async_callback(self.annotations.close)

    @staticmethod
    def _prompt_text(prompt: list[PromptBlock]) -> str:
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
    from .active_route import ActiveRoute, resolve_comms_route

    LaunchRoot.startup(sys.argv, ActiveRoute.published_launch)
    with resolve_comms_route().hold_client_route():
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
        # The stdio ACP client only attaches to a separately owned worker. A
        # private launch is checked before it can request an owner; the worker
        # independently repeats the same preflight.
        comms = wire()
        comms.owners.require_launch_authority()

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
            )

            def observe(event: StreamEvent) -> None:
                agent._debug_log(
                    f"{event.direction.value}: {json.dumps(event.message)[:200]}"
                )

            try:
                output_stream, input_stream = await stdio_streams(
                    limit=DEFAULT_STDIO_BUFFER_LIMIT_BYTES
                )
                connection = AcpController(
                    agent, input_stream, output_stream, listening=False,
                    use_unstable_protocol=agent.use_unstable_protocol,
                    observers=[observe] if os.environ.get("AGENT_COMMS_DEBUG_LOG") else None,
                )
                try:
                    await connection.listen()
                finally:
                    await asyncio.shield(connection.close())
            finally:
                await agent.shutdown()

        asyncio.run(run())
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
