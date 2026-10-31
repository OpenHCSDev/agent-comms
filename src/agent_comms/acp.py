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
import sys
import time
from collections.abc import AsyncIterator, Iterator, Sequence
from contextlib import AsyncExitStack, asynccontextmanager, contextmanager
from functools import partial
from typing import TYPE_CHECKING

from acp import RequestError
from acp.agent.connection import AgentSideConnection
from acp.connection import StreamEvent
from acp.core import DEFAULT_STDIO_BUFFER_LIMIT_BYTES
from acp.schema import (
    AgentMessageChunk,
    ClientCapabilities,
    Implementation,
    InitializeResponse,
    LoadSessionRequest,
    LoadSessionResponse,
    NewSessionRequest,
    NewSessionResponse,
    PromptRequest,
    PromptResponse,
    PermissionOption,
    RequestPermissionResponse,
    ToolCallUpdate,
    SetSessionConfigOptionResponse,
    TextContentBlock,
)
from acp.stdio import stdio_streams
from pydantic import BaseModel, JsonValue, ValidationError

from agent_comms.coordination_errors import IdentityConflict, Refused
from agent_comms.coordinator import Coordination

from . import (
    acp_extension,
    manual_compaction_bridge,
)
from .acp_extension import (
    encode_updates,
)
from .acp_failure import NativeTurnError
from .acp_request_consumer import AcpRequestConsumer
from .active_route import ActiveRoute
from .agent_backend import CompactionOutcome
from .channel_targets import NamedTarget
from .cohort_foreground import _accept_visible_deliveries
from .comms import Comms, wire
from .controller import (
    ACP_PERMISSION_TIMEOUT_SECONDS, Controller, SessionUpdateSink,
)
from .coordinated_runtime import SelectedExecution
from .coordination_cohort import next_sealed_assignment
from .coordination_database import CoordinationStore
from .cursor_owner import CursorOwner
from .cursor_publication import CursorPublication
from .nominal.field_codec import AcpSessionId, ClaimId, FieldCodec, RootId
from .image_inputs import prompt_images
from .input_drain import InputDrain
from .config_options import ConfigOptions
from .owner_launch import RetainedOwnerLaunch
from .input_effects import InputEffects
from .launch_root import LaunchRoot
from .message_bus import MessageBus
from .publisher import ResponseWrites
from .queued_input import DeliveredInput
from .runtime_server import RuntimeServer
from .owner_connection import socket_path
from .runtime import RuntimeProxy
from .selected_write_plan import SelectedWritePlans
from .session_effects import SessionEffects
from .session_lifecycle import AttachedSessionLifecycle, SessionLifecycle
from .threads import Thread
from .turn_effects import TurnEffects
from .turn_runner import TurnRunner
from .working_memory_policy import AnnotationPolicy
from .working_memory_worker import AnnotationWorker


from dataclasses import dataclass
from .drain_failures import drain_failures
from .activity import FailedWakeDiagnostic
from agent_comms.input_drain import SchedulesWake, WakeDue, WakeHeld, WakeOutcome
from .thread_identity import OwnerIdentity

from agent_comms.selected_write_authority import BoundSelectedWriteAuthority
if TYPE_CHECKING:
    from .selected_tool_broker import SelectedToolIntent
    from .coordination_tables.assignments import WakeAssignment

from .native_arguments import ConfiguredBinary, InheritedArguments, LaunchArguments, LaunchBinary
from .turn_runner import RELAY_PREFIX


@dataclass
class AcpSelectedWriteAuthority(BoundSelectedWriteAuthority):
    agent: CommsAgent
    session_id: str
    plans: SelectedWritePlans

    def select(self, assignment, owner, generation, action):
        plan = self.plans.load(assignment, owner, generation)
        if plan is None:
            return action
        self.require_current(assignment, owner, plan.operation_id)
        return action.with_plan(self, plan, assignment, owner)

    def require_current(
        self, assignment: WakeAssignment, owner: Thread, operation_id: ClaimId
    ) -> None:
        bound = self.agent._selected_write_controllers.get((owner.name, assignment.wire_seq))
        if bound is None or bound[0] != operation_id:
            raise IdentityConflict("Selected write original controller is no longer bound")
        controller = bound[1]
        controller.require_current(self, self.session_id)

    @contextmanager
    def writing(
        self, assignment: WakeAssignment, owner: Thread, operation_id: ClaimId
    ) -> Iterator[None]:
        self.require_current(assignment, owner, operation_id)
        try:
            yield
            self.plans.applied(assignment, owner, operation_id)
        finally:
            # An accepted intent is never retried, so a failed write ends its binding too.
            del self.agent._selected_write_controllers[(owner.name, assignment.wire_seq)]


@dataclass(frozen=True, kw_only=True)
class SelectedWakeCheck(SchedulesWake):
    """A selected turn runs beside the owner's observation, which hands it later messages."""

    execution: SelectedExecution
    thread: str
    owner: OwnerIdentity

    async def run(self) -> WakeOutcome:
        inputs, session_id = self.inputs, self.session_id
        effects = inputs.effects
        root_id = inputs.comms.owners.require_private_launch().wire_root_id
        # Selection may have been scheduled just before maintenance began.
        # A deferred selection is not a successful drain or diagnostic recovery.
        hold = await Coordination.run_worker(inputs.comms.owners.maintenance.read)
        if hold.turn_pending:
            return WakeHeld(hold)
        async with drain_failures(inputs.comms, self.thread, self.owner, FailedWakeDiagnostic):
            before = await inputs._observe_private_revision(session_id, root_id)
            result = await effects.turns.run_selected(session_id, self.execution)
            if result is not None:
                # A disconnected client must not turn a settled claim into an
                # apparent model failure. Reconnect reads the same durable row.
                await effects.cursors.publish(
                    session_id, self.thread, selected_status=result.cursor_status
                )
            else:
                # N (or absent-audience) rows prove coverage, not an injected
                # input. Extend only an existing current generation or an all-N prefix;
                # old-generation Pi evidence cannot initialize this cursor on reconnect.
                await effects.cursors.publish(session_id, self.thread, advance=True)
                if before == await inputs._observe_private_revision(session_id, root_id):
                    inputs.state(session_id).idle_revision = before
            if result is not None:
                # Messages this turn could not take are still pending.
                await inputs.drain_inbox(session_id)
        return WakeDue()


class SdkSessionConnection(AgentSideConnection, Controller, SessionUpdateSink):
    """The SDK connection and controller publish one Core acceptance answer."""

    async def publish_update(self, *, session_id: AcpSessionId, update: BaseModel) -> bool:
        await self.session_update(session_id=session_id, update=update)
        return True


    def require_current(self, authority: AcpSelectedWriteAuthority, session_id: AcpSessionId) -> None:
        if self is not authority.agent.sessions.client:
            raise IdentityConflict("Selected write ACP controller changed")

    async def request_ui_permission(
        self, runner: TurnRunner, session_id: AcpSessionId,
        request: tuple[ToolCallUpdate, list[PermissionOption]],
    ) -> dict[str, object] | None:
        tool_call, options = request
        if self is runner.sessions.client:
            response = await asyncio.wait_for(
                self.request_permission(
                    session_id=session_id, tool_call=tool_call, options=options
                ),
                timeout=ACP_PERMISSION_TIMEOUT_SECONDS,
            )
            return RequestPermissionResponse.model_validate(response).outcome.model_dump(
                by_alias=True, exclude_none=True
            )
        return None

    def departed(self, runtime: RuntimeServer, session_id: AcpSessionId) -> bool:
        return False


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

        self.annotations = AnnotationWorker(comms, self._runtime, self, AnnotationPolicy.configured())
        self.cursors = CursorPublication(comms, self._runtime)
        self.turns = TurnRunner(
            comms,
            self._runtime,
            self,
        )
        self.sessions = self.session_lifecycle_class(
            comms, self._runtime, runtime_enabled, self
        )
        self.inputs = InputDrain(
            comms, self.sessions, self._runtime, self, auto_wake,
            ConfigOptions(RetainedOwnerLaunch.configured(agent_bin, agent_args), comms.route),
        )
        self.turns.bind(self.sessions, self.inputs)
        # A preplanned write is valid only while its original ACP controller
        # remains attached. Never restore this binding after a process crash.
        self._selected_write_controllers: dict[tuple[str, int], tuple[ClaimId, Controller]] = {}

    def _create_runtime_proxy(self, thread: Thread, session_id: AcpSessionId) -> RuntimeProxy:
        return RuntimeProxy(
            self, thread.incarnation, session_id,
            socket_path(self._comms.root, thread.require_process().pid),
        )

    # Private attachment ABI consumed by runtime/compaction. State exists only
    # on its component; in-process consumers use that owner directly.

    def on_connect(self, client: SdkSessionConnection) -> None:
        """Called by AgentSideConnection with the client-facing connection."""
        self.sessions.client = client

    # ─── ACP methods ─────────────────────────────────────────────────────────

    @staticmethod
    @contextmanager
    def refusals() -> Iterator[None]:
        """Core exceptions acquire protocol form only when answering an ACP request."""
        try:
            yield
        except Refused as error:
            raise RequestError(error.code, error.message, error.data) from error
        except NativeTurnError as error:
            raise RequestError.internal_error(error.request.error_data()) from error.__cause__

    async def initialize(
        self,
        protocol_version: int,
        client_capabilities: ClientCapabilities | None = None,
        client_info: Implementation | None = None,
    ) -> InitializeResponse:
        with self.refusals():
            return await self.sessions.initialize(protocol_version, client_capabilities, client_info)

    @staticmethod
    def _sdk_request[Request: BaseModel](
        model: type[Request], /, **parameters: JsonValue | Sequence[BaseModel]
    ) -> Request:
        """Reassemble SDK fields and expanded metadata using the SDK declaration."""
        metadata = parameters.pop("_meta", None) or parameters.pop("field_meta", None) or {}
        if not isinstance(metadata, dict):
            raise RequestError.invalid_params(
                {"reason": "ACP metadata must be an object"}
            )
        fields = {name: value for name, value in parameters.items() if name in model.model_fields}
        expanded = {
            name: value for name, value in parameters.items() if name not in model.model_fields
        }
        try:
            return model.model_validate({**fields, "field_meta": {**metadata, **expanded}})
        except ValidationError as error:
            raise RequestError.invalid_params({"reason": str(error)}) from error

    async def new_session(
        self, cwd: str, mcp_servers: Sequence[BaseModel] | None = None, **kwargs: JsonValue
    ) -> NewSessionResponse:
        request = self._sdk_request(
            NewSessionRequest,
            cwd=cwd,
            mcp_servers=[] if mcp_servers is None else mcp_servers,
            **kwargs,
        )
        with self.refusals():
            return await self.sessions.new_session(request)

    async def load_session(
        self,
        cwd: str,
        session_id: str,
        mcp_servers: Sequence[BaseModel] | None = None,
        **kwargs: JsonValue,
    ) -> LoadSessionResponse:
        request = self._sdk_request(
            LoadSessionRequest,
            cwd=cwd,
            session_id=session_id,
            mcp_servers=[] if mcp_servers is None else mcp_servers,
            **kwargs,
        )
        with self.refusals():
            return await self.sessions.load_session(request)

    async def prompt(
        self, session_id: str, prompt: Sequence[BaseModel], **kwargs: JsonValue
    ) -> PromptResponse:
        request = self._sdk_request(PromptRequest, session_id=session_id, prompt=prompt, **kwargs)
        return await self.receive_prompt(request)

    async def receive_prompt(self, request: PromptRequest) -> PromptResponse:
        with self.refusals():
            session_id = FieldCodec.decode(AcpSessionId, request.session_id)
            return await AcpRequestConsumer(self, request, session_id).run()

    async def _compact_request(self, session_id: AcpSessionId, instructions: str | None) -> PromptResponse:
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

    async def _selected_write_request(self, session_id: AcpSessionId, request) -> PromptResponse:
        owner = self.sessions.require(session_id)
        controller = self._runtime.controller.get()
        attached = controller.attached(self.sessions.client) if controller is not None else None
        if attached is None:
            raise RequestError.invalid_params(
                {"reason": "Selected write requires attached ACP controller"}
            )
        scope = await Coordination.run_worker(partial(self.cursors.scope, owner, session_id))
        plans = SelectedWritePlans(self._comms, scope.require_live().wire_root_id)
        acknowledgement = await Coordination.run_worker(
            partial(
                plans.submit,
                owner_name=owner,
                source_seq=request.source_seq,
                source_message_id=request.source_message_id,
                resource=request.resource,
                contents=request.contents,
            )
        )
        self._selected_write_controllers[(owner, request.source_seq)] = (
            acknowledgement.operation_id,
            attached,
        )
        return PromptResponse(stop_reason="end_turn", field_meta=encode_updates(acknowledgement))

    async def _prompt_request(
        self, ingress: AcpRequestConsumer, command: acp_extension.PromptRequest
    ) -> PromptResponse:
        session_id = ingress.session_id
        request = ingress.request
        text = self._prompt_text(request)
        display_text = command.user_text or text
        try:
            images = prompt_images(request)
        except ValueError as error:
            raise RequestError.invalid_params({"reason": str(error)}) from error
        if images and text.lstrip().startswith((*NamedTarget.sigils(), RELAY_PREFIX)):
            raise RequestError.invalid_params(
                {"reason": "Send images to an agent thread, not as a coordination relay."}
            )
        if (
            (await self.turns.active_backend_turn(session_id)).is_open
            and not text.lstrip().startswith((*NamedTarget.sigils(), RELAY_PREFIX))
        ):
            return await self.inputs.accept_followup(
                session_id,
                text=text,
                display_text=display_text,
                request=command,
                images=images,
            )
        self.inputs.ensure_live_drain(session_id)
        async with self.inputs.state(session_id).turn_lock:
            # A direct ACP prompt owns this one controller. Owner-socket prompts
            # already carry a private subscriber binding (including None when
            # absent); never fall back to a passive ACP client in that case.
            existing = self._runtime.controller.get()
            context = (
                self._runtime.controller.set(self.sessions.client)
                if existing is not None and existing.attached(None) is None
                else None
            )
            try:
                return await self.turns.prompt_owned(
                    session_id,
                    text=text,
                    images=images,
                    display_text=display_text,
                    input_id=command.input_id,
                    origin=command.origin,
                )
            finally:
                if context is not None:
                    self._runtime.controller.reset(context)

    @staticmethod
    def _require_compaction_text(request: PromptRequest) -> None:
        if len(request.prompt) != 1:
            raise RequestError.invalid_params(
                {"reason": "/compact requires one text block."}
            )
        if not isinstance(request.prompt[0], TextContentBlock):
            raise RequestError.invalid_params(
                {"reason": "/compact requires text only."}
            )

    async def emit_session_identity(
        self, session_id: AcpSessionId, name: str, client: SessionUpdateSink,
    ) -> None:
        """Let a subscriber identify its owner before potentially long replay."""
        await client.publish_update(
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
        with self.refusals():
            return await self.turns.cancel(FieldCodec.decode(AcpSessionId, session_id), **kwargs)

    async def close_session(self, session_id: str, **kwargs: JsonValue):
        with self.refusals():
            return await self.sessions.close_session(FieldCodec.decode(AcpSessionId, session_id))

    async def set_config_option(
        self, config_id: str, session_id: str, value: str | bool, **kwargs: JsonValue
    ) -> SetSessionConfigOptionResponse:
        with self.refusals():
            return await self.sessions.config.set_option(config_id, FieldCodec.decode(AcpSessionId, session_id), value)

    async def authenticate(self, method_id: str, **kwargs: JsonValue) -> None:
        raise RequestError.auth_required({"reason": "agent-comms requires no authentication"})

    # ─── Helpers ─────────────────────────────────────────────────────────────

    async def _drain_private_nk(self, session_id: AcpSessionId, wire_root_id: RootId) -> int:
        """Run a selected private wake for this ACP session.

        This explicitly configured path reuses the reviewed one-shot native
        reservation/send boundary. No schema/participant is installed here;
        both must already belong to the same private root. A failing or
        uncertain native turn propagates and cannot be replayed by a drain.
        """
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
                bus,
                wire_root_id,
                store_path,
                lookup,
                owner_name=owner.name,
                acquire_package=execution.validate,
            )
        # Acceptance visited the originals; pending work still starts at what
        # the native history proves consumed, including work below the frontier.
        async with delivery.reading() as source_reads:
            state = await CursorOwner.read_async(
                bus,
                wire_root_id=wire_root_id,
                owner_name=thread_name,
                source_reads=source_reads,
            )
        after_seq = state.covered_seq
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

    def stop_all(self) -> None:
        """Request every executing session's own stop; attachments have no execution."""
        for state in self.inputs.drained.values():
            state.request_stop()

    @asynccontextmanager
    async def host(self, listener_fd: int) -> AsyncIterator[None]:
        """One host owns its inherited listener and all session cleanup."""
        try:
            self._runtime.inherit_listener(listener_fd)
            await self._runtime.start()
            yield
        finally:
            self.stop_all()
            await self.shutdown()

    async def shutdown(self) -> None:
        """Stop drains and mark threads owned by this ACP connection offline."""
        async with AsyncExitStack() as retirement:
            # CursorPublication owns the cross-observation evidence lifetime;
            # close it after its session/input producers have drained.
            retirement.callback(self.cursors.close)
            retirement.callback(self.inputs.end_sessions)
            retirement.push_async_callback(self.sessions.release_owned)
            retirement.push_async_callback(self.turns.close)
            retirement.push_async_callback(self.sessions.close_proxies)
            retirement.push_async_callback(self.inputs.close)
            retirement.push_async_callback(self.annotations.close)

    @staticmethod
    def _prompt_text(request: PromptRequest) -> str:
        return "\n".join(
            block.text
            for block in request.prompt
            if isinstance(block, TextContentBlock) and block.text
        )


class CommsClient(CommsAgent):
    """A stdio connection attaches to a separately owned executor."""

    session_lifecycle_class = AttachedSessionLifecycle


def main() -> int:
    root = LaunchRoot.startup(sys.argv, ActiveRoute.published_launch)
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
    comms = wire(root.path)
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
            connection = SdkSessionConnection(
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
