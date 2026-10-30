"""State-owning ACP attachment lifecycle; turn and delivery effects stay external."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import os
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING, cast

from .coordination_errors import Refused
from acp.schema import (
    AgentCapabilities,
    ClientCapabilities,
    CloseSessionResponse,
    Implementation,
    InitializeResponse,
    LoadSessionRequest,
    LoadSessionResponse,
    NewSessionRequest,
    NewSessionResponse,
    PromptCapabilities,
    SessionCapabilities,
    SessionCloseCapabilities,
    SessionInfoUpdate,
    TerminalAuthMethod,
)
from pydantic import JsonValue

from . import agent_events as events
from .acp_extension import (
    CoordinationChangedUpdate,
    GoalChangedUpdate,
    encode_updates,
)
from .child_process import ProcessIdentity, join_retirement
from .comms import Comms
from .config_options import ConfigOptions
from .input_key import InputKey, InputKeyText
from .launch_runtime import ThreadLaunch
from .owner_launch import RetainedOwnerLaunch
from .turn_input_source import NoTurnAcceptance
from .session_config_options import SessionConfigOptions
from .coordinator import Coordination
from .errors import RelationViolationError
from .field_codec import AcpSessionId, FieldCodec, MalformedRecord, boundary_decoder
from .owner_protocol import IncompatibleOwner, IncompatibleOwnerError
from .pi_rule_gate import PiRedeal
from .registry_document import RegistryEntry
from .runtime import RuntimeProxy, RuntimeServer
from .runtime_requests import FreshSubscribeRuntimeRequest, SubscribeRuntimeRequest
from .session_effects import SessionEffects
from .session_load import ExistingSessionLoadPlan, FailedSessionLoadPlan, SessionLoadPlan
from .thread_identity import AcpThreadRole, ClaimedIdentity, OwnerIdentity
from .thread_model import ThreadModel
from .thread_status import ActiveThreadPresence
from .thread_text import NoThreadText, ThreadText
from .repo import Checkout
from .threads import Thread
from .transcript_updates import TranscriptReplay


if TYPE_CHECKING:
    from .acp import SdkSessionConnection
    from .input_drain import OwnerSession


@dataclass(frozen=True)
class PublishedSession:
    """The thread metadata a session last published, derived from its Thread."""

    name: str
    title: ThreadText
    worktree: Checkout

    @classmethod
    def of(cls, thread: Thread) -> PublishedSession:
        return cls(thread.name, thread.title, thread.worktree)


class SessionLifecycle:
    def __init__(
        self,
        comms: Comms,
        runtime: RuntimeServer,
        runtime_enabled: bool,
        effects: SessionEffects,
    ):
        self.comms = comms
        self.runtime, self.runtime_enabled, self.effects = runtime, runtime_enabled, effects
        self.published: dict[AcpSessionId, PublishedSession] = {}
        self.client: SdkSessionConnection | None = None
        self.proxies: dict[str, RuntimeProxy | IncompatibleOwner] = {}
        self._attachment_lock = asyncio.Lock()
        self._identity_lock = asyncio.Lock()
        self.transcript = TranscriptReplay(comms)
        self.config = SessionConfigOptions(comms, runtime, self, effects)

    async def initialize(
        self,
        protocol_version: int,
        client_capabilities: ClientCapabilities | None = None,
        client_info: Implementation | None = None,
    ) -> InitializeResponse:
        methods: list[TerminalAuthMethod] = []
        if (
            client_capabilities is not None
            and client_capabilities.auth is not None
            and client_capabilities.auth.terminal
        ):
            methods = [
                TerminalAuthMethod(
                    type="terminal",
                    id="pi-login",
                    name="Connect a provider",
                    description="Choose a provider and sign in using Pi's native UI",
                    args=["--login"],
                )
            ]
        return InitializeResponse(
            protocol_version=protocol_version,
            agent_capabilities=AgentCapabilities(
                load_session=True,
                prompt_capabilities=PromptCapabilities(image=True),
                session_capabilities=self.session_capabilities(),
            ),
            agent_info=Implementation(name="agent-comms", title="Agent Comms", version="0.1.0"),
            auth_methods=methods,
        )

    def session_capabilities(self) -> SessionCapabilities:
        return SessionCapabilities()

    async def close_session(self, session_id: str) -> CloseSessionResponse:
        # An executing owner is not an attachment. Its turn/input retirement
        # remains with shutdown, rather than treating tab close as owner stop.
        raise Refused.method_not_found("session/close")

    def require(self, session_id: str) -> str:
        if session_id not in self.published:
            raise Refused.invalid_params({"reason": f"Unknown sessionId: {session_id!r}"})
        return self.published[AcpSessionId(session_id)].name

    def binds(self, session_id: str, name: object) -> bool:
        """Whether this session is published as the named thread."""
        return session_id in self.published and self.published[AcpSessionId(session_id)].name == name

    def require_owned_session(self, entry: RegistryEntry) -> AcpSessionId:
        """Resolve an original loaded resource; registration grants no attachment."""
        for session_id, published in self.published.items():
            if entry.matches_name(published.name):
                return session_id
        raise Refused.invalid_params({
            "reason": f"Registered owner {entry.thread.name!r} has no loaded ACP session. "
                      "PID registration does not attach a session.",
            "owner": entry.thread.name,
        })

    @staticmethod
    def reject_foreign_mcp(request: NewSessionRequest | LoadSessionRequest) -> None:
        if request.mcp_servers:
            raise Refused.invalid_params(
                {
                    "reason": (
                        "ACP mcpServers are unsupported; use Pi's native MCP package configuration."
                    )
                }
            )

    @boundary_decoder
    def declare_thread(self, cwd: str) -> Thread:
        """Decode the ACP directory and register before choosing its executor."""
        checkout = FieldCodec.decode(Checkout, cwd)
        thread = self.comms.threads.claim_thread(
            checkout.path.name,
            tags=frozenset({"acp"}),
            worktree=checkout,
            model=ThreadModel.selected_by(self.effects.inputs.configuration.agent_args),
            thinking_level=self.effects.inputs.configuration.agent_args.thinking,
            auto_title_pending=True,
            role=AcpThreadRole,
        )
        self.comms.registry.restore_stopped(self.comms.registry.snapshot(), (thread.name,))
        return self.acquire_declared_thread(thread)

    def acquire_declared_thread(self, thread: Thread) -> Thread:
        """An owning ACP lifecycle acquires its calling process after publication."""
        return self.comms.owners.acquire_thread(thread.name)

    def validated_thread(self, cwd: Checkout, session_id: str) -> Thread:
        thread = self.comms.registry.require(session_id)
        thread.execution.require_native()
        if not thread.contains_worktree(cwd, self.comms.registry.storage_rule):
            raise Refused.invalid_params(
                {"reason": "The saved thread belongs to a different working directory."}
            )
        return thread

    async def bind_owned(self, thread: Thread, session_id: str) -> None:
        owner = (await Coordination.run_worker(partial(
            self.comms.registry.entry, thread.name,
        ))).entry.owner_identity
        await self.bind_owner(thread, AcpSessionId(session_id), self.effects.inputs.configuration, owner)

    async def bind_owner(
        self, thread: Thread, session_id: AcpSessionId,
        configuration: ConfigOptions, owner: OwnerIdentity,
    ) -> OwnerSession:
        async with self._attachment_lock:
            await self.retire_proxy(session_id)
        state = self.effects.inputs.acquire_owner(session_id, configuration, owner)
        await Coordination.run_worker(PiRedeal.of(self.comms, thread).start)
        self.published[session_id] = PublishedSession.of(thread)
        if self.runtime_enabled:
            await self.runtime.start()
        return state

    async def new_session(self, request: NewSessionRequest) -> NewSessionResponse:
        self.reject_foreign_mcp(request)
        await Coordination.run_worker(self.comms.owners.require_private_launch)
        thread = await Coordination.run_worker(partial(self.declare_thread, request.cwd))
        await self.bind_owned(thread, thread.name)
        self.effects.inputs.ensure_live_drain(thread.name)
        response = NewSessionResponse(
            session_id=thread.name,
            field_meta=await self.metadata(thread.name, session_id=thread.name),
        )
        self.config.start_menu(thread.name, self.runtime)
        return response

    async def load_session(self, request: LoadSessionRequest) -> LoadSessionResponse:
        self.reject_foreign_mcp(request)
        session_id = request.session_id
        thread = await self._acquire_loaded(FieldCodec.decode(Checkout, request.cwd), request.session_id)
        if not thread.process_identity.runs_here():
            return await self.attach_owner(thread, session_id)
        await self._bind_loaded(thread, session_id)
        await self.transcript.replay(session_id, self.runtime)
        self.effects.inputs.ensure_live_drain(session_id)
        response = LoadSessionResponse(
            field_meta=await self.metadata(thread.name, session_id=session_id),
        )
        self.config.start_menu(session_id, self.runtime)
        return response

    async def start_owner(self, launch: RetainedOwnerLaunch, owner: OwnerIdentity) -> None:
        """Attach the reserved lease using that owner's original launch, not the host ingress."""
        if not launch.process.runs_here():
            raise RelationViolationError("The launch does not name this worker host")
        selected = ThreadLaunch.current(launch.environment)
        entry = (await Coordination.run_worker(partial(
            self.comms.registry.entry, owner.incarnation.name,
        ))).entry
        thread = entry.require_owner_process(owner, launch.process)
        if not entry.matches_name(selected.name):
            raise RelationViolationError("Owner environment names another thread")
        session_id = AcpSessionId(thread.name)
        state = await self.bind_owner(
            thread, session_id, ConfigOptions(launch, self.comms.route), owner)
        self.effects.inputs.ensure_live_drain(str(session_id))
        key = selected.take_startup_input(launch.environment)
        if key:
            self.runtime.background(self.run_startup_input(state, InputKeyText.decode(key)))

    async def run_startup_input(self, state: OwnerSession, key: InputKey) -> None:
        """Consume only the explicitly authorized first input; never scan or replay."""
        session_id = str(state.identifier)
        async with state.turn_lock:
            async with state.own_turn_task():
                def capture() -> str:
                    snapshot = self.comms.registry.snapshot()
                    thread = snapshot.require(state.owner.incarnation.name)
                    return self.effects.inputs.dispositions.lookup(key).queued_text(
                        thread.incarnation, snapshot.claim_generation(thread.name))
                text = await Coordination.run_worker(capture)
                await self.effects.inputs.emit_input_delivery_changed(session_id)
                await self.effects.turns.run_agent_turn(
                    session_id, session_id, text, acceptance=NoTurnAcceptance(),
                    original_keys=(key,), original_owner_input=True,
                )

    async def complete_unheld(self, expected: OwnerIdentity, claim: ClaimedIdentity) -> None:
        """A fenced owner whose session this host never held (an aborted start) completes
        here, the process its row is bound to."""
        await Coordination.run_worker(partial(
            self.comms.owners.complete_session, expected, expected,
            ProcessIdentity.capture(os.getpid()), claim.claimed_generation,
        ))

    async def retire_owner(self, state: OwnerSession, expected: OwnerIdentity, claim: ClaimedIdentity) -> None:
        async with state.retirement_lock:
            if state.retired.done():
                await state.retired
                return
            try:
                state.request_stop()
                self.runtime.retire_session(state.identifier)
                await self.effects.inputs.close_sessions((state,))
                await self.effects.turns.close_sessions((state,))
                await Coordination.run_worker(partial(
                    self.comms.owners.complete_session, state.owner, expected,
                    state.configuration.launch.process, claim.claimed_generation,
                ))
                self.config.stop_observing_configuration(state.identifier)
                del self.published[state.identifier]
                self.effects.inputs.end_session(state.identifier)
            except BaseException as error:
                state.retired.set_exception(error)
                raise
            else:
                state.retired.set_result(None)

    async def _acquire_loaded(self, cwd: Checkout, session_id: str) -> Thread:
        await Coordination.run_worker(self.comms.owners.require_private_launch)
        thread = await Coordination.run_worker(partial(self.validated_thread, cwd, session_id))
        if not (
            self.binds(session_id, thread.name)
            and thread.process_identity.runs_here()
            and (await Coordination.run_worker(partial(self.comms.registry.status, thread.name)))
                .items_with(ActiveThreadPresence, thread)
        ):
            thread = await Coordination.run_worker(partial(
                self.comms.owners.acquire_thread, thread.name,
            ))
        return thread

    async def _bind_loaded(self, thread: Thread, session_id: str) -> None:
        await self.bind_owned(thread, session_id)
        await self.effects.inputs.emit_input_delivery_changed(session_id)

    async def attach_owner(
        self, thread: Thread, session_id: str,
        subscription: type[SubscribeRuntimeRequest] = SubscribeRuntimeRequest,
    ) -> LoadSessionResponse:
        async with self._attachment_lock:
            await self.retire_proxy(session_id)
            snapshot = await Coordination.run_worker(self.comms.registry.snapshot)
            binding = snapshot.owner_binding(thread.name)
            failed_command = FailedSessionLoadPlan(binding)
            proxy = await Coordination.run_worker(partial(self.effects._create_runtime_proxy, thread, session_id))
            try:
                snapshot.require_owner_process(snapshot.owner_identity(thread.name), thread.require_process())
                metadata = await proxy.subscribe(subscription)
                self.proxies[session_id] = proxy
            except IncompatibleOwnerError as error:
                # History is Core's own store; only owner requests need the owner's Core.
                await self.transcript.replay(session_id, self.runtime)
                self.proxies[session_id] = error.owner
                return LoadSessionResponse(field_meta={
                    **await self.metadata(thread.name, session_id=session_id),
                    **error.owner.failure_metadata(), **ExistingSessionLoadPlan(binding).metadata(),
                })
            except (OSError, RuntimeError, RelationViolationError, MalformedRecord) as error:
                raise Refused.invalid_params(
                    {"reason": (
                        f"Unable to attach to {thread.name!r} owner {thread.process_identity}: {error}"
                     ), **failed_command.failure_metadata()}
                ) from error
            finally:
                if self.proxies.get(session_id) is not proxy:
                    await proxy.close()
            return LoadSessionResponse(
                field_meta={**metadata, **ExistingSessionLoadPlan(binding).metadata()},
            )

    async def sync_identity(self, session_id: str) -> str:
        """Publish each observed identity generation once across concurrent consumers."""
        async with self._identity_lock:
            cached_name = self.require(session_id)
            entry = (await Coordination.run_worker(partial(
                self.comms.registry.entry, cached_name,
            ))).entry
            thread = entry.thread
            name = thread.name
            if (
                name != cached_name
                or not thread.process_identity.runs_here()
                or not entry.status.running
            ):
                await self.effects.turns.close_idle_backend(session_id, thread)
            current = PublishedSession.of(thread)
            if self.published[AcpSessionId(session_id)] != current:
                await self.publish_metadata(session_id, title=thread.title)
                self.published[AcpSessionId(session_id)] = current
            return name

    @boundary_decoder
    async def publish_metadata(
        self, session_id: str, *, title: ThreadText = NoThreadText(),
    ) -> None:
        """Encode the thread's title once for the ACP metadata boundary."""
        await self.runtime.publish_update(
            session_id=session_id,
            update=SessionInfoUpdate(
                session_update="session_info_update",
                title=cast(str | None, FieldCodec.encode(title)),
                field_meta=await self.metadata(self.require(session_id), session_id=session_id),
            ),
        )

    async def observe_native_configuration(
        self, session_id: str, thread_name: str, event: events.NativeMetadataObservation
    ) -> None:
        """Initialize only unset configuration from its actual native producer."""
        await Coordination.run_worker(partial(
            self.comms.threads.initialize_native_configuration,
            thread_name, model=event.model, thinking_level=event.thinking_level,
        ))

    def configuration_updates(
        self, thread_name: str,
    ) -> tuple[CoordinationChangedUpdate, GoalChangedUpdate]:
        """Capture original store projections before loop/client effects consume them."""
        snapshot = GoalChangedUpdate.from_snapshot(self.comms.goals.goal_snapshot(thread_name))
        return self.comms.configuration.answer(thread_name), snapshot

    async def metadata(self, thread_name: str, *, session_id: str | None = None) -> dict[str, JsonValue]:
        configuration = await Coordination.run_worker(partial(
            self.configuration_updates, thread_name,
        ))
        return encode_updates(
            *configuration,
            await self.effects.inputs.queue_state(session_id or thread_name),
            *await self.effects.cursors.trusted_metadata(thread_name, session_id or thread_name),
        )

    async def retire_proxy(self, session_id: str) -> None:
        """Keep the proxy registered until its reader is closed and awaited."""
        proxy = self.proxies.get(session_id)
        if proxy is not None:
            retirement = asyncio.create_task(proxy.close())
            try:
                await join_retirement(retirement)
            finally:
                if retirement.done() and not retirement.cancelled() and retirement.exception() is None:
                    del self.proxies[session_id]
                    self.effects.inputs.end_session(session_id)

    async def close_proxies(self) -> None:
        async with self._attachment_lock:
            for session_id in tuple(self.proxies):
                await self.retire_proxy(session_id)

    async def release_owned(self) -> None:
        """Release every owned thread, then report each release that failed.

        This includes a thread this process was launched to own but failed to
        bind: otherwise a startup failure leaves it registered as running with
        a dead process. One failed stop must not keep the others registered as
        running, and it must not disappear either: the failures surface
        together after the runtime has closed.
        """
        failures: list[Exception] = []
        try:
            names = {
                owner.owner.incarnation.name
                for state in self.effects.inputs.drained.values()
                for owner in state.executors()
            }
            owners = await Coordination.run_worker(partial(self.comms.owners.fence_stop, *names))
            requests = await Coordination.run_worker(partial(self.comms.owners.stop_requests, owners))
            for request in requests:
                try:
                    entry = (await Coordination.run_worker(partial(
                        self.comms.registry.entry, request.thread))).entry
                    state = self.effects.inputs.state(self.require_owned_session(entry)).owned()
                    await request.retire(self, state)
                except (OSError, ValueError) as error:
                    failures.append(error)
        finally:
            await self.config.close()
            await self.runtime.close()
        if failures:
            raise ExceptionGroup("Owned threads were not released at shutdown", failures)


class AttachedSessionLifecycle(SessionLifecycle):
    """A stdio attachment requests a separate executor; it never claims ownership."""

    def session_capabilities(self) -> SessionCapabilities:
        return SessionCapabilities(close=SessionCloseCapabilities()) if (
            self.effects.use_unstable_protocol
        ) else super().session_capabilities()

    async def close_session(self, session_id: str) -> CloseSessionResponse:
        if not self.effects.use_unstable_protocol:
            raise Refused.method_not_found("session/close")
        async with self._attachment_lock:
            if session_id not in self.proxies:
                raise Refused.invalid_params({"reason": "Unknown attached session"})
            await self.retire_proxy(session_id)
        return CloseSessionResponse()

    def acquire_declared_thread(self, thread: Thread) -> Thread:
        """Leave the registered declaration for the load operation to check."""
        return thread

    async def new_session(self, request: NewSessionRequest) -> NewSessionResponse:
        self.reject_foreign_mcp(request)
        thread = await Coordination.run_worker(partial(self.declare_thread, request.cwd))
        loaded = await self._attach_requested_session(
            thread,
            thread.name,
            (request.field_meta or {}).get("agentCommsLoad"),
            FreshSubscribeRuntimeRequest,
        )
        return NewSessionResponse(
            session_id=thread.name,
            field_meta=loaded.field_meta,
        )

    async def load_session(self, request: LoadSessionRequest) -> LoadSessionResponse:
        self.reject_foreign_mcp(request)
        session_id = request.session_id
        thread = await Coordination.run_worker(
            partial(self.validated_thread, FieldCodec.decode(Checkout, request.cwd), request.session_id)
        )
        return await self._attach_requested_session(
            thread,
            session_id,
            (request.field_meta or {}).get("agentCommsLoad"),
            SubscribeRuntimeRequest,
        )

    async def _attach_requested_session(
        self, thread: Thread, session_id: str, load_metadata: object,
        subscription: type[SubscribeRuntimeRequest],
    ) -> LoadSessionResponse:
        try:
            plan = SessionLoadPlan.at_ingress(load_metadata)
            owner = await plan.resolve(self, thread)
        except (OSError, RuntimeError, RelationViolationError, MalformedRecord) as error:
            raise Refused.invalid_params(
                {"reason": f"Session load not authorized for {thread.name!r}: {error}"}
            ) from error
        return await self.attach_owner(owner, session_id, subscription)
