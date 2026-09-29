"""Configuration owns its catalogs, generations and setting correlations."""

from __future__ import annotations

import asyncio
import os
from abc import abstractmethod
from dataclasses import replace
from typing import TYPE_CHECKING, Any, ClassVar
from uuid import uuid4

from acp import RequestError
from acp.schema import (
    ConfigOptionUpdate,
    SessionConfigOptionSelect,
    SessionConfigSelectOption,
    SetSessionConfigOptionResponse,
)

from . import agent_events as events
from . import backend
from .comms import Comms
from .declared_family import DeclaredFamily
from .native_arguments import NativeArguments
from .pending_requests import PendingRequests
from .pi_commands import (
    GetAvailableModels,
    GetAvailableThinkingLevels,
    SetModel,
    SetThinkingLevel,
    SettingCommand,
)
from .runtime import RuntimeServer
from .session_effects import SessionEffects
from .threads import Thread

if TYPE_CHECKING:
    from .session_lifecycle import SessionLifecycle


class ConfigOption(DeclaredFamily, affix="ConfigOption"):
    title: ClassVar[str]
    description: ClassVar[str]
    category: ClassVar[str]

    @abstractmethod
    def current_value(self, thread: Thread) -> str | None: ...

    @abstractmethod
    async def discover(self, thread: Thread) -> list[SessionConfigSelectOption]: ...

    @abstractmethod
    async def describe(self, owner: ConfigOptions, thread: Thread) -> SessionConfigOptionSelect: ...

    @abstractmethod
    async def change(
        self, owner: ConfigOptions, session_id: str, thread: Thread, value: str
    ) -> None: ...


class CatalogConfigOption(ConfigOption):
    """A selected option owns its catalog, auth revision, lock and generation."""

    def __init__(self, agent_bin: str, agent_args: NativeArguments):
        self.agent_bin, self.agent_args = agent_bin, agent_args
        self.catalogs: dict[str | None, list[SessionConfigSelectOption]] = {}
        self.auth: tuple[int, int] | None = None
        self.lock = asyncio.Lock()
        self.generation = 0

    @property
    def current_auth(self) -> bool:
        return self.auth == backend.auth_revision()

    def cache_key(self, thread: Thread) -> str | None:
        return thread.model

    async def choices(self, thread: Thread) -> list[SessionConfigSelectOption]:
        async with self.lock:
            if not self.current_auth:
                self.catalogs.clear()
                self.auth = backend.auth_revision()
            key = self.cache_key(thread)
            if key not in self.catalogs:
                self.catalogs[key] = await self.discover(thread)
                self.generation += 1
            return self.catalogs[key]

    def selection(self, owner: ConfigOptions, thread: Thread, choices) -> str:
        return self.current_value(thread)

    async def describe(self, owner: ConfigOptions, thread: Thread) -> SessionConfigOptionSelect:
        choices = await self.choices(thread)
        selected = self.selection(owner, thread, choices)
        if selected not in {choice.value for choice in choices}:
            choices = [SessionConfigSelectOption(value=selected, name=selected), *choices]
        return SessionConfigOptionSelect(
            id=self.declared_name,
            name=self.title,
            description=self.description,
            category=self.category,
            type="select",
            current_value=selected,
            options=choices,
        )

    @abstractmethod
    async def apply(
        self, owner: ConfigOptions, session_id: str, thread: Thread, value: str
    ) -> None: ...

    async def change(
        self, owner: ConfigOptions, session_id: str, thread: Thread, value: str
    ) -> None:
        options = await self.describe(owner, thread)
        if value not in {choice.value for choice in options.options}:
            raise RequestError.invalid_params(
                {"reason": f"Unknown {self.title.lower()}: {value!r}"}
            )
        await self.apply(owner, session_id, thread, value)


class ModelConfigOption(CatalogConfigOption):
    title = "Model"
    description = "Model used by this persistent agent thread"
    category = "model"

    def current_value(self, thread: Thread) -> str | None:
        return thread.model

    def cache_key(self, thread: Thread) -> None:
        return None

    async def discover(self, thread: Thread) -> list[SessionConfigSelectOption]:
        values = [
            value.strip()
            for value in os.environ.get("AGENT_COMMS_AGENT_MODELS", "").split(",")
            if value.strip()
        ]
        if not values:
            data = await GetAvailableModels().discover(self.agent_bin, self.agent_args.argv)
            values = [model.display_name for model in data.models if model.provider and model.id]
        return [
            SessionConfigSelectOption(value=value, name=value) for value in dict.fromkeys(values)
        ]

    async def apply(
        self, owner: ConfigOptions, session_id: str, thread: Thread, value: str
    ) -> None:
        provider, model = value.split("/", 1)
        await owner.set_active_backend_option(
            session_id, SetModel(provider=provider, model_id=model), "Model change timed out"
        )
        owner.comms.threads.set_thread_model(thread.name, value)


class ThinkingLevelConfigOption(CatalogConfigOption):
    title = "Thinking level"
    description = "Reasoning effort used by this persistent agent thread"
    category = "thought_level"

    def current_value(self, thread: Thread) -> str | None:
        return thread.thinking_level

    async def discover(self, thread: Thread) -> list[SessionConfigSelectOption]:
        if os.environ.get("AGENT_COMMS_AGENT_MODELS"):
            levels = ["off", "minimal", "low", "medium", "high"]
        else:
            data = await GetAvailableThinkingLevels().discover(
                self.agent_bin, self.agent_args.with_model(thread.model).argv
            )
            levels = data.levels or ["off"]
        return [SessionConfigSelectOption(value=level, name=level.title()) for level in levels]

    def selection(self, owner: ConfigOptions, thread: Thread, choices) -> str:
        selected = self.current_value(thread)
        levels = {choice.value for choice in choices}
        if selected not in levels:
            selected = "medium" if "medium" in levels else choices[0].value
            self.persist(owner, thread, selected)
        return selected

    async def apply(
        self, owner: ConfigOptions, session_id: str, thread: Thread, value: str
    ) -> None:
        await owner.set_active_backend_option(
            session_id, SetThinkingLevel(level=value), "Thinking level change timed out"
        )
        self.persist(owner, thread, value)

    def persist(self, owner: ConfigOptions, thread: Thread, value: str) -> None:
        owner.comms.threads.set_thread_thinking_level(thread.name, value)


class ConfigOptions:
    def __init__(
        self,
        comms: Comms,
        agent_bin: str,
        agent_args: NativeArguments,
        runtime: RuntimeServer,
        sessions: SessionLifecycle,
        effects: SessionEffects,
    ):
        self.comms, self.agent_bin, self.agent_args = comms, agent_bin, agent_args
        self.runtime, self.sessions, self.effects = runtime, sessions, effects
        self.catalogs: dict[type[CatalogConfigOption], CatalogConfigOption] = {}
        self.catalog_publish_lock = asyncio.Lock()
        self.session_catalog_generation: dict[str, int] = {}
        self.session_config_signature: dict[str, tuple[tuple[str, str | None], ...]] = {}
        self.setting_requests = PendingRequests()

    def catalog_for(self, member: type[CatalogConfigOption]) -> CatalogConfigOption:
        if member not in self.catalogs:
            self.catalogs[member] = member(self.agent_bin, self.agent_args)
        return self.catalogs[member]

    @property
    def catalog_generation(self) -> int:
        return sum(option.generation for option in self.catalogs.values())

    def ensure_thread_model(self, thread_name: str) -> str | None:
        thread = self.comms.registry.require(thread_name)
        selected = thread.model
        if selected is None:
            selected = self.comms.threads.resolve_thread_model(
                thread.name, self.agent_args.model
            )
        if selected is not None and selected != thread.model:
            self.comms.threads.set_thread_model(thread.name, selected)
        return selected

    async def options(self, thread_name: str) -> list[Any]:
        if self.ensure_thread_model(thread_name) is None:
            return []
        thread = self.comms.registry.require(thread_name)
        return [
            await self.catalog_for(member).describe(self, thread)
            for member in ConfigOption.members_with(CatalogConfigOption)
        ]

    def signature(self, thread: Thread) -> tuple[tuple[str, str | None], ...]:
        return tuple(
            (member.declared_name, self.catalog_for(member).current_value(thread))
            for member in ConfigOption.members_with(CatalogConfigOption)
        )

    async def session_options(self, session_id: str, thread_name: str) -> list[Any]:
        options = await self.options(thread_name)
        self.session_catalog_generation[session_id] = self.catalog_generation
        thread = self.comms.registry.require(thread_name)
        self.session_config_signature[session_id] = self.signature(thread)
        return options

    async def refresh_auth_models(self) -> None:
        if not self.catalogs:
            return
        if all(option.current_auth for option in self.catalogs.values()) and all(
            self.session_catalog_generation.get(sid) == self.catalog_generation
            for sid in self.sessions.bindings
        ):
            return
        async with self.catalog_publish_lock:
            for sid, name in tuple(self.sessions.bindings.items()):
                options = await self.options(name)
                if self.session_catalog_generation.get(sid) == self.catalog_generation:
                    continue
                await self.publish(sid, options)
                self.session_catalog_generation[sid] = self.catalog_generation

    async def publish(self, session_id: str, options: list[Any]) -> None:
        await self.runtime.session_update(
            session_id=session_id,
            update=ConfigOptionUpdate(
                session_update="config_option_update", config_options=options
            ),
        )

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
        await self.catalog_for(member).change(
            self, session_id, self.comms.registry.require(name), value
        )
        await self.effects.turns.close_idle_backend(session_id)
        options = await self.options(name)
        await self.publish(session_id, options)
        return SetSessionConfigOptionResponse(config_options=options)

    async def set_active_backend_option(
        self,
        session_id: str,
        command: SettingCommand,
        timeout_message: str,
    ) -> None:
        inbox = self.effects.turns.active_backend_inbox(session_id)
        if inbox is None:
            return
        request_id = uuid4().hex
        future = self.setting_requests.add(command.result_type, request_id)
        inbox.put_nowait(replace(command, id=request_id).to_rpc())
        try:
            await asyncio.wait_for(future, timeout=10)
        except (TimeoutError, RuntimeError) as error:
            raise RequestError.invalid_params({"reason": str(error) or timeout_message}) from error
        finally:
            self.setting_requests.discard(command.result_type, request_id)

    async def sync_thread(self, session_id: str) -> None:
        name = await self.sessions.sync_identity(session_id)
        await self.effects.turns.goals.sync_goal_execution(session_id, name)
        thread = self.comms.registry.require(name)
        signature = self.signature(thread)
        if self.session_config_signature.get(session_id) == signature:
            return
        self.session_config_signature[session_id] = signature
        if self.sessions.client is None and not self.sessions.runtime_enabled:
            return
        await self.publish(session_id, await self.options(name))

    async def observe_agent_info(
        self, session_id: str, thread_name: str, event: events.AgentInfo
    ) -> None:
        """Apply first-observed settings from S1/S2's typed backend observation."""
        if event.model and self.comms.registry.require(thread_name).model is None:
            self.comms.threads.set_thread_model(thread_name, event.model)
            await self.publish(session_id, await self.options(thread_name))
        if event.thinking_level and self.comms.registry.require(thread_name).thinking_level is None:
            self.comms.threads.set_thread_thinking_level(thread_name, event.thinking_level)
