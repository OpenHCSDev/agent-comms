"""State-owning ACP attachment lifecycle; turn and delivery effects stay external."""

from __future__ import annotations

import asyncio
import os
import re
from functools import partial
from pathlib import Path
from typing import Any

from acp import RequestError
from acp.schema import (
    AgentCapabilities,
    CloseSessionResponse,
    Implementation,
    InitializeResponse,
    LoadSessionResponse,
    NewSessionResponse,
    PromptCapabilities,
    SessionInfoUpdate,
    SessionCapabilities,
    SessionCloseCapabilities,
    TerminalAuthMethod,
)

from .field_codec import MalformedRecord
from . import agent_events as events
from .acp_extension import (
    CoordinationChangedUpdate,
    GoalChangedUpdate,
    encode_updates,
)
from .comms import Comms
from .child_process import join_retirement
from .coordinator import Coordination
from .errors import RelationViolationError
from .config_options import SessionConfigOptions
from .native_arguments import NativeArguments
from .runtime import RuntimeProxy, RuntimeServer
from .session_effects import SessionEffects
from .threads import Thread
from .registry_document import RegistryEntry
from .transcript_updates import TranscriptReplay
from .owner_protocol import IncompatibleOwner, IncompatibleOwnerError
from .session_load import SessionLoadPlan, ExistingSessionLoadPlan, FailedSessionLoadPlan


class SessionLifecycle:
    def __init__(
        self,
        comms: Comms,
        agent_bin: str,
        agent_args: NativeArguments,
        runtime: RuntimeServer,
        runtime_enabled: bool,
        effects: SessionEffects,
    ):
        self.comms, self.agent_bin, self.agent_args = comms, agent_bin, agent_args
        self.runtime, self.runtime_enabled, self.effects = runtime, runtime_enabled, effects
        self.bindings: dict[str, str] = {}
        # A launcher registers this process as the owner before it starts, so
        # this process owns that registration even if it never binds a session.
        self.launched_owners: set[str] = set()
        self.client: Any = None
        self.titles: dict[str, str] = {}
        self.display_titles: dict[str, str | None] = {}
        self.worktrees: dict[str, str] = {}
        self.proxies: dict[str, RuntimeProxy | IncompatibleOwner] = {}
        self._attachment_lock = asyncio.Lock()
        self._identity_lock = asyncio.Lock()
        self.transcript = TranscriptReplay(comms, runtime)
        self.config = SessionConfigOptions(comms, agent_bin, agent_args, runtime, self, effects)

    async def initialize(
        self,
        protocol_version: int,
        client_capabilities: Any = None,
        client_info: Any = None,
    ) -> InitializeResponse:
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
        if (capabilities.get("auth") or {}).get("terminal"):
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
        raise RequestError.method_not_found("session/close")

    @staticmethod
    def thread_name_for(cwd: str) -> str:
        leaf = re.sub(r"[^A-Za-z0-9_-]+", "-", Path(cwd).name or "session").strip("-")
        return leaf or "session"

    def require(self, session_id: str) -> str:
        name = self.bindings.get(session_id)
        if name is None:
            raise RequestError.invalid_params({"reason": f"Unknown sessionId: {session_id!r}"})
        return name

    def require_owned_session(self, entry: RegistryEntry) -> str:
        """Resolve an original loaded resource; registration grants no attachment."""
        for session_id, name in self.bindings.items():
            if entry.matches_name(name):
                return session_id
        raise RequestError.invalid_params({
            "reason": f"Registered owner {entry.thread.name!r} has no loaded ACP session. "
                      "PID registration does not attach a session.",
            "owner": entry.thread.name,
        })

    @staticmethod
    def reject_foreign_mcp(mcp_servers: list[Any] | None) -> None:
        if mcp_servers is not None and (type(mcp_servers) is not list or mcp_servers):
            raise RequestError.invalid_params(
                {
                    "reason": (
                        "ACP mcpServers are unsupported; use Pi's native MCP package configuration."
                    )
                }
            )

    def declare_thread(self, cwd: str) -> Thread:
        """Register the participant before choosing which process executes it."""
        thread = self.comms.threads.claim_thread(
            self.thread_name_for(cwd),
            tags=frozenset({"acp"}),
            worktree=cwd,
            model=self.agent_args.model,
            thinking_level=self.agent_args.thinking,
            auto_title_pending=True,
        )
        self.comms.threads.restore_stopped(self.comms.registry.snapshot(), (thread.name,))
        return self.acquire_declared_thread(thread)

    def acquire_declared_thread(self, thread: Thread) -> Thread:
        """An owning ACP lifecycle acquires its calling process after publication."""
        return self.comms.owners.acquire_thread(thread.name, owner_pid=os.getpid())

    def validated_thread(self, cwd: str, session_id: str) -> Thread:
        thread = self.comms.registry.require(session_id)
        thread.execution.require_native()
        if not thread.contains_worktree(cwd, self.comms.registry.storage_rule):
            raise RequestError.invalid_params(
                {"reason": "The saved thread belongs to a different working directory."}
            )
        return thread

    async def bind_owned(self, thread: Thread, session_id: str) -> None:
        async with self._attachment_lock:
            await self.retire_proxy(session_id)
        self.bindings[session_id] = thread.name
        self.titles[session_id] = thread.name
        self.worktrees[session_id] = thread.worktree
        if self.runtime_enabled:
            await self.runtime.start()

    async def new_session(
        self, cwd: str, mcp_servers: list[Any] | None = None, **kwargs: Any
    ) -> NewSessionResponse:
        self.reject_foreign_mcp(mcp_servers)
        await Coordination.run_worker(self.effects._private_nk_marker)
        thread = await Coordination.run_worker(partial(self.declare_thread, cwd))
        await self.bind_owned(thread, thread.name)
        self.effects.inputs.ensure_live_drain(thread.name)
        async with self.config.session_options(thread.name) as options:
            return NewSessionResponse(
                session_id=thread.name,
                config_options=options,
                field_meta=await self.metadata(thread.name, session_id=thread.name),
            )

    async def load_session(
        self, cwd: str, session_id: str, mcp_servers: list[Any] | None = None, **kwargs: Any
    ) -> LoadSessionResponse:
        self.reject_foreign_mcp(mcp_servers)
        thread = await self._acquire_loaded(cwd, session_id)
        if not thread.process_identity.runs_here():
            return await self.attach_owner(thread, session_id)
        await self._bind_loaded(thread, session_id)
        async with self.config.session_options(thread.name) as options:
            self.effects.inputs.ensure_live_drain(session_id)
            return LoadSessionResponse(
                config_options=options,
                field_meta=await self.metadata(thread.name, session_id=session_id),
            )

    async def start_owner(self, cwd: str, session_id: str) -> None:
        """Take a headless owner's session; no ACP consumer awaits a load response.

        Configuration choices and metadata belong to a consumer's load or
        subscription, which discovers them on demand. The owner process does
        not launch catalog children only to discard their answer.
        """
        self.launched_owners.add(session_id)
        thread = await self._acquire_loaded(cwd, session_id)
        if not thread.process_identity.runs_here():
            raise RelationViolationError(
                f"Headless owner {os.getpid()} did not acquire {thread.name!r}; "
                f"it belongs to {thread.process_identity}"
            )
        from .attempt_recovery import RecoveryMonitorCapability

        await Coordination.run_worker(partial(
            RecoveryMonitorCapability.recover_owner, self.comms.root, thread,
        ))
        await self._bind_loaded(thread, session_id)
        self.effects.inputs.ensure_live_drain(session_id)

    async def _acquire_loaded(self, cwd: str, session_id: str) -> Thread:
        await Coordination.run_worker(self.effects._private_nk_marker)
        thread = await Coordination.run_worker(partial(self.validated_thread, cwd, session_id))
        if not (
            self.bindings.get(session_id) == thread.name
            and thread.process_identity.runs_here()
            and (await Coordination.run_worker(partial(self.comms.registry.status, thread.name))).active
        ):
            thread = await Coordination.run_worker(partial(
                self.comms.owners.acquire_thread, thread.name, owner_pid=os.getpid(),
            ))
        return thread

    async def _bind_loaded(self, thread: Thread, session_id: str) -> None:
        await Coordination.run_worker(partial(self.comms.threads.heartbeat, thread.name))
        await self.bind_owned(thread, session_id)
        await self.transcript.replay(session_id, thread.name)
        await self.effects.inputs.emit_input_delivery_changed(session_id)

    async def attach_owner(self, thread: Thread, session_id: str) -> LoadSessionResponse:
        async with self._attachment_lock:
            await self.retire_proxy(session_id)
            snapshot = await Coordination.run_worker(self.comms.registry.snapshot)
            binding = snapshot.owner_binding(thread.name)
            failed_command = FailedSessionLoadPlan(binding)
            proxy = await Coordination.run_worker(partial(self.effects._create_runtime_proxy, thread, session_id))
            try:
                snapshot.require_owner_process(snapshot.owner_identity(thread.name), thread.require_process())
                metadata = await proxy.subscribe()
                self.proxies[session_id] = proxy
            except IncompatibleOwnerError as error:
                # History is Core's own store; only owner requests need the owner's Core.
                await self.transcript.replay(session_id, thread.name)
                self.proxies[session_id] = error.owner
                return LoadSessionResponse(field_meta={
                    **await self.metadata(thread.name, session_id=session_id),
                    **error.owner.failure_metadata(), **ExistingSessionLoadPlan(binding).metadata(),
                })
            except (OSError, RuntimeError, RelationViolationError, MalformedRecord) as error:
                raise RequestError.invalid_params(
                    {"reason": (
                        f"Unable to attach to {thread.name!r} owner {thread.process_identity}: {error}"
                     ), **failed_command.failure_metadata()}
                ) from error
            finally:
                if self.proxies.get(session_id) is not proxy:
                    await proxy.close()
            return LoadSessionResponse(
                config_options=metadata.pop("configOptions", []),
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
            self.bindings[session_id] = name
            if (
                self.titles.get(session_id) != name
                or self.display_titles.get(session_id) != thread.title
                or self.worktrees.get(session_id) != thread.worktree
            ):
                await self.runtime.session_update(
                    session_id=session_id,
                    update=SessionInfoUpdate(
                        session_update="session_info_update",
                        title=thread.title,
                        field_meta=await self.metadata(name, session_id=session_id),
                    ),
                )
                self.titles[session_id], self.display_titles[session_id] = name, thread.title
                self.worktrees[session_id] = thread.worktree
            return name

    async def observe_native_configuration(
        self, session_id: str, thread_name: str, event: events.NativeMetadataObservation
    ) -> None:
        """Initialize only unset configuration from its actual native producer."""
        await Coordination.run_worker(partial(
            self.comms.threads.initialize_native_configuration,
            thread_name, model=event.model, thinking_level=event.thinking_level,
        ))
        await self.config.publish_configuration(session_id, thread_name)

    def configuration_updates(
        self, thread_name: str,
    ) -> tuple[CoordinationChangedUpdate, GoalChangedUpdate]:
        """Capture original store projections before loop/client effects consume them."""
        snapshot = GoalChangedUpdate.from_snapshot(self.comms.goals.goal_snapshot(thread_name))
        return self.comms.configuration.answer(thread_name), snapshot

    async def metadata(self, thread_name: str, *, session_id: str | None = None) -> dict[str, Any]:
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

    async def close_proxies(self) -> None:
        async with self._attachment_lock:
            for session_id in tuple(self.proxies):
                await self.retire_proxy(session_id)

    def release_registered_owner(self, name: str) -> None:
        snapshot = self.comms.registry.snapshot()
        thread = snapshot.require(name)
        if thread.process_identity.runs_here() and snapshot.status(thread.name).running:
            self.comms.owners.stop(thread.name)

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
            for name in set(self.bindings.values()) | self.launched_owners:
                try:
                    await Coordination.run_worker(partial(self.release_registered_owner, name))
                except (OSError, ValueError) as error:
                    failures.append(error)
        finally:
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
            raise RequestError.method_not_found("session/close")
        async with self._attachment_lock:
            if session_id not in self.proxies:
                raise RequestError.invalid_params({"reason": "Unknown attached session"})
            await self.retire_proxy(session_id)
        return CloseSessionResponse()

    def acquire_declared_thread(self, thread: Thread) -> Thread:
        """Leave the registered declaration for the load operation to check."""
        return thread

    async def new_session(
        self, cwd: str, mcp_servers: list[Any] | None = None, **kwargs: Any
    ) -> NewSessionResponse:
        self.reject_foreign_mcp(mcp_servers)
        thread = await Coordination.run_worker(partial(self.declare_thread, cwd))
        loaded = await self.load_session(cwd, thread.name, mcp_servers, **kwargs)
        return NewSessionResponse(
            session_id=thread.name,
            config_options=loaded.config_options,
            field_meta=loaded.field_meta,
        )

    async def load_session(
        self, cwd: str, session_id: str, mcp_servers: list[Any] | None = None, **kwargs: Any
    ) -> LoadSessionResponse:
        self.reject_foreign_mcp(mcp_servers)
        thread = await Coordination.run_worker(partial(self.validated_thread, cwd, session_id))
        try:
            plan = SessionLoadPlan.at_ingress(kwargs.get("agentCommsLoad"))
            owner = await plan.resolve(self, thread)
        except (OSError, RuntimeError, RelationViolationError, MalformedRecord) as error:
            raise RequestError.invalid_params(
                {"reason": f"Session load not authorized for {thread.name!r}: {error}"}
            ) from error
        return await self.attach_owner(owner, session_id)
