"""State-owning ACP attachment lifecycle; turn and delivery effects stay external."""

from __future__ import annotations

import asyncio
import os
import re
from functools import partial
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from acp import RequestError
from acp.schema import (
    AgentCapabilities,
    ConfigOptionUpdate,
    SessionConfigOptionSelect,
    SessionConfigSelectOption,
    SetSessionConfigOptionResponse,
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
from .config_options import CatalogConfigOption, CatalogDescription, CatalogSelection, ConfigOptions, InvalidConfigChoice
from .agent_backend import UseSettings
from .field_codec import FieldCodec
from .native_errors import NativePiUnavailable
from .pi_vocabulary import ThinkingLevel
from .native_arguments import NativeArguments
from .runtime import RuntimeProxy, RuntimeServer
from .session_effects import SessionEffects
from .threads import Thread
from .registry_document import RegistrySnapshot
from .thread_identity import RecordedThreadIncarnation
from .transcript_updates import TranscriptReplay
from .session_load import SessionLoadAdmission, ExistingSessionLoadAdmission, FailedSessionLoadAdmission


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
        self.bindings: dict[str, str] = {}
        # A launcher registers this process as the owner before it starts, so
        # this process owns that registration even if it never binds a session.
        self.owned_incarnations: set[RecordedThreadIncarnation] = set()
        self.client: Any = None
        self.titles: dict[str, str] = {}
        self.display_titles: dict[str, str | None] = {}
        self.worktrees: dict[str, str] = {}
        self.proxies: dict[str, RuntimeProxy] = {}
        self._attachment_lock = asyncio.Lock()
        self._identity_lock = asyncio.Lock()
        self.transcript = TranscriptReplay(comms, runtime)
        self.config = SessionConfigOptions(comms, runtime, self, effects)

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

    def require_owned_session(self, thread: Thread, snapshot: RegistrySnapshot) -> str:
        """Resolve an original loaded resource; registration grants no attachment."""
        for session_id, name in self.bindings.items():
            if snapshot.canonical_name(name) == thread.name:
                return session_id
        raise RequestError.invalid_params({
            "reason": f"Registered owner {thread.name!r} has no loaded ACP session. "
                      "PID registration does not attach a session.",
            "owner": thread.name,
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
        """Publish the participant before this lifecycle resolves execution custody."""
        thread = self.comms.threads.claim_thread(
            self.thread_name_for(cwd),
            tags=frozenset({"acp"}),
            worktree=cwd,
            model=self.comms.settings.model,
            thinking_level=self.comms.settings.thinking_level,
            auto_title_pending=True,
        )
        self.comms.threads.restore_stopped(self.comms.registry.snapshot(), (thread.name,))
        return self.acquire_declared_thread(thread)

    def acquire_declared_thread(self, thread: Thread) -> Thread:
        """An owning ACP lifecycle acquires its calling process after publication."""
        return self.acquire_original(thread.incarnation)

    def acquire_original(self, incarnation: RecordedThreadIncarnation) -> Thread:
        thread = self.comms.owners.acquire_thread(incarnation, owner_pid=os.getpid())
        if thread.pid == os.getpid():
            self.owned_incarnations.add(thread.incarnation)
        return thread

    def validated_thread(self, cwd: str, session_id: str) -> Thread:
        thread = self.comms.registry.require(session_id)
        thread.execution.require_native()
        if not thread.contains_worktree(cwd):
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
        if thread.pid != os.getpid():
            return await self.attach_owner(thread, session_id)
        await self._bind_loaded(thread, session_id)
        async with self.config.session_options(thread.name) as options:
            self.effects.inputs.ensure_live_drain(session_id)
            return LoadSessionResponse(
                config_options=options,
                field_meta=await self.metadata(thread.name, session_id=session_id),
            )

    async def start_owner(self, cwd: str, incarnation: RecordedThreadIncarnation) -> None:
        """Take a headless owner's session; no ACP consumer awaits a load response.

        Configuration choices and metadata belong to a consumer's load or
        subscription, which discovers them on demand. The owner process does
        not launch catalog children only to discard their answer.
        """
        self.owned_incarnations.add(incarnation)
        session_id = incarnation.name
        await Coordination.run_worker(self.effects._private_nk_marker)
        await Coordination.run_worker(partial(self.validated_thread, cwd, session_id))
        thread = await Coordination.run_worker(partial(
            self.acquire_original, incarnation,
        ))
        if thread.pid != os.getpid():
            raise RelationViolationError(
                f"Headless owner {os.getpid()} did not acquire {thread.name!r}; it belongs to {thread.pid}"
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
            and thread.pid == os.getpid()
            and (await Coordination.run_worker(partial(self.comms.registry.status, thread.name))).active
        ):
            thread = await Coordination.run_worker(partial(
                self.acquire_original, thread.incarnation,
            ))
        return thread

    async def _bind_loaded(self, thread: Thread, session_id: str) -> None:
        await Coordination.run_worker(partial(self.comms.threads.heartbeat, thread.name))
        await self.bind_owned(thread, session_id)
        await self.transcript.replay(session_id, thread.name)
        await self.effects.inputs.replay_unknown_inputs(session_id)

    async def attach_owner(self, thread: Thread, session_id: str) -> LoadSessionResponse:
        async with self._attachment_lock:
            await self.retire_proxy(session_id)
            snapshot = await Coordination.run_worker(self.comms.registry.snapshot)
            binding = snapshot.owner_binding(thread.name)
            failed_command = FailedSessionLoadAdmission(binding)
            proxy = await Coordination.run_worker(partial(self.effects._create_runtime_proxy, thread, session_id))
            try:
                snapshot.require_owner_process(snapshot.owner_identity(thread.name), thread.require_process())
                metadata = await proxy.subscribe()
                self.proxies[session_id] = proxy
            except (OSError, RuntimeError, TypeError, ValueError) as error:
                raise RequestError.invalid_params(
                    {"reason": f"Unable to attach to {thread.name!r} owner {thread.pid}: {error}",
                     **failed_command.failure_metadata()}
                ) from error
            finally:
                if self.proxies.get(session_id) is not proxy:
                    await proxy.close()
            return LoadSessionResponse(
                config_options=metadata.pop("configOptions", []),
                field_meta={**metadata, **ExistingSessionLoadAdmission(binding).metadata()},
            )

    async def sync_identity(self, session_id: str) -> str:
        """Publish each observed identity generation once across concurrent consumers."""
        async with self._identity_lock:
            cached_name = self.require(session_id)
            snapshot = await Coordination.run_worker(self.comms.registry.snapshot)
            thread = snapshot.require(cached_name)
            name = thread.name
            if (
                name != cached_name
                or thread.pid != os.getpid()
                or not snapshot.status(name).running
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
                        title=thread.title or name,
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
        goal, execution = self.comms.goals.goal_snapshot(thread_name)
        return self.comms.configuration.answer(thread_name), GoalChangedUpdate(goal, execution)

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
        """Keep custody until the original reader is closed and joined."""
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

    def release_registered_owner(self, incarnation: RecordedThreadIncarnation) -> None:
        self.comms.owners.release(incarnation.name, caller=incarnation)

    async def release_owned(self) -> None:
        """Release every owned thread, then report each release that failed.

        This includes a thread this process was launched to own but failed to
        bind: otherwise a startup failure leaves it registered as running with
        a dead process. One failed stop must not keep the others registered as
        running, and it must not disappear either: the failures surface
        together after the runtime has closed.
        """
        failures: list[Exception] = []
        for incarnation in tuple(self.owned_incarnations):
            try:
                await Coordination.run_worker(partial(self.release_registered_owner, incarnation))
            except Exception as error:
                failures.append(error)
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
        """Leave the published declaration for the original load admission."""
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
            admission = SessionLoadAdmission.at_ingress(kwargs.get("agentCommsLoad"))
            owner = await admission.resolve(self, thread)
        except (OSError, RuntimeError, TypeError, ValueError) as error:
            raise RequestError.invalid_params(
                {"reason": f"Session load not admitted for {thread.name!r}: {error}"}
            ) from error
        return await self.attach_owner(owner, session_id)


class SessionConfigOptions(ConfigOptions):
    """Add ACP conversion and session publication to the Core catalogs."""

    def __init__(
        self,
        comms: Comms,
        transport: RuntimeServer,
        sessions: SessionLifecycle,
        effects: SessionEffects,
    ):
        super().__init__(comms)
        self.transport, self.sessions, self.effects = transport, sessions, effects
        self.catalog_publish_lock = asyncio.Lock()
        self.publications: dict[str, ConfigOptionUpdate] = {}
        # The stored settings each session's owner last saw, to notice a change.
        # The model and thinking level each running turn currently uses.
        self.settings_in_use: dict[str, UseSettings] = {}


    async def options(self, thread: CatalogSelection) -> list[SessionConfigOptionSelect]:
        try:
            descriptions = await self.descriptions(thread)
        except InvalidConfigChoice as error:
            raise RequestError.invalid_params({"reason": str(error)}) from error
        return [self.advertisement(description) for description in descriptions]

    @staticmethod
    def advertisement(description: CatalogDescription) -> SessionConfigOptionSelect:
        declaration = description.declaration
        choices = [SessionConfigSelectOption(value=choice.value, name=choice.name,
                                             description=choice.description)
                   for choice in description.choices]
        selected = description.current_value
        if selected is None:
            selected = ""
            choices.insert(0, SessionConfigSelectOption(value=selected, name="Not configured"))
        return SessionConfigOptionSelect(
            id=FieldCodec.encode(declaration),
            name=declaration.title,
            description=declaration.description,
            category=declaration.category,
            type="select",
            current_value=selected,
            options=choices,
        )

    @asynccontextmanager
    async def session_options(
        self, thread_name: str
    ) -> AsyncIterator[list[SessionConfigOptionSelect]]:
        """Hold the configuration observation until its consumer supplies it.

        A private new/load/Ready reply does not publish to other subscribers.
        Its snapshot must not follow a newer broadcast on the same transport.
        """
        async with self.catalog_publish_lock:
            thread = await Coordination.run_worker(partial(self.comms.registry.require, thread_name))
            yield await self.options(thread)

    async def _publish(self, session_id: str, options: list[SessionConfigOptionSelect]) -> None:
        """Publish within the original session_options acquisition lifetime."""
        update = ConfigOptionUpdate(
            session_update="config_option_update", config_options=options
        )
        if self.publications.get(session_id) == update:
            return
        if await self.transport.session_update(session_id=session_id, update=update):
            # Retain exactly what this send supplied, not a later catalog
            # generation or settings observation acquired during its await.
            self.publications[session_id] = update

    async def set_option(
        self, config_id: str, session_id: str, value: str | bool
    ) -> SetSessionConfigOptionResponse:
        try:
            member = CatalogConfigOption.decode(config_id)
            if not isinstance(value, str):
                raise ValueError("Expected string setting")
        except ValueError:
            raise RequestError.invalid_params(
                {"reason": f"Unknown session configuration option: {config_id!r}"}
            ) from None
        if session_id in self.sessions.proxies:
            result = await self.sessions.proxies[session_id].request(
                "set_config_option", config_id=config_id, value=value
            )
            return SetSessionConfigOptionResponse.model_validate(result)
        name = await self.sessions.sync_identity(session_id)
        thread = await Coordination.run_worker(partial(self.comms.registry.require, name))
        try:
            await self.catalog_for(member).change(self, thread, value)
        except (NativePiUnavailable, InvalidConfigChoice) as error:
            raise RequestError.invalid_params({"reason": str(error)}) from error
        await self.effects.turns.close_idle_backend(session_id, thread)
        await self.use_stored_settings(session_id, name)
        async with self.session_options(name) as options:
            await self._publish(session_id, options)
            return SetSessionConfigOptionResponse(config_options=options)


    @staticmethod
    def stored_settings(thread: Thread) -> UseSettings:
        return UseSettings(thread.model, ThinkingLevel.optional_name(thread.thinking_level))

    def turn_started(self, session_id: str, thread: Thread) -> None:
        self.settings_in_use.setdefault(session_id, self.stored_settings(thread))

    async def use_stored_settings(self, session_id: str, name: str) -> None:
        """A running turn switches at its next model call, whichever process stored the change."""
        thread = await Coordination.run_worker(partial(self.comms.registry.require, name))
        settings = self.stored_settings(thread)
        if self.settings_in_use.get(session_id, settings) == settings:
            return
        inbox = await self.effects.turns.active_backend_inbox(session_id)
        if inbox is not None:
            self.settings_in_use[session_id] = settings
            inbox.put_nowait(settings)

    async def sync_thread(self, session_id: str) -> None:
        name = await self.sessions.sync_identity(session_id)
        await self.effects.turns.goals.sync_goal_execution(session_id, name)
        await self.use_stored_settings(session_id, name)
        await self.publish_configuration(session_id, name)

    async def publish_configuration(self, session_id: str, thread_name: str) -> None:
        if self.sessions.client is None and not self.sessions.runtime_enabled:
            return
        async with self.session_options(thread_name) as options:
            await self._publish(session_id, options)
