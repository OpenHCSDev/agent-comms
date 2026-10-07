"""Configuration owns its catalogs, generations and setting correlations."""

from __future__ import annotations

import asyncio
import logging
import os
from abc import abstractmethod
from dataclasses import replace
from functools import partial
from typing import TYPE_CHECKING, Any, ClassVar
from uuid import uuid4

from acp import RequestError
from acp.schema import (
    ConfigOptionUpdate,
    SessionConfigOptionSelect,
    SessionConfigSelectOption,
    SetSessionConfigOptionResponse,
)

from .pi_vocabulary import ThinkingLevel
from . import backend
from .comms import Comms
from .coordinator import Coordination
from .declared_family import DeclaredFamily
from .field_codec import FieldCodec
from .native_arguments import NativeArguments
from .native_pi import NativePiUnavailable
from .owner_launch import RestartEnvironment
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
    async def describe(self, thread: Thread) -> SessionConfigOptionSelect: ...

    @abstractmethod
    async def change(
        self, owner: ConfigOptions, session_id: str, thread: Thread, value: str
    ) -> None: ...


class CatalogConfigOption(ConfigOption):
    """A selected option owns its catalog, auth revision, lock and generation."""

    def __init__(self, agent_bin: str, agent_args: NativeArguments, configuration: RestartEnvironment):
        self.agent_bin, self.agent_args = agent_bin, agent_args
        self.configuration = configuration
        self.catalogs: dict[str | None, list[SessionConfigSelectOption]] = {}
        self.auth: tuple[int, int] | None = None
        self.lock = asyncio.Lock()
        self.generation = 0

    @property
    def current_auth(self) -> bool:
        return self.auth == self.configuration.auth_revision()

    def cache_key(self, thread: Thread) -> str | None:
        return thread.model

    async def choices(self, thread: Thread) -> list[SessionConfigSelectOption]:
        async with self.lock:
            if not self.current_auth:
                self.catalogs.clear()
                self.auth = self.configuration.auth_revision()
            key = self.cache_key(thread)
            if key not in self.catalogs:
                try:
                    choices = await self.discover(thread)
                except NativePiUnavailable as error:
                    logging.getLogger(__name__).warning("%s catalog unavailable: %s", self.title, error)
                    return []
                self.catalogs[key] = choices
                self.generation += 1
            return self.catalogs[key]

    async def describe(self, thread: Thread) -> SessionConfigOptionSelect:
        choices = await self.choices(thread)
        configured = self.current_value(thread)
        selected = configured if configured is not None else ""
        if selected not in {choice.value for choice in choices}:
            choices = [SessionConfigSelectOption(
                value=selected, name=configured if configured is not None else "Not configured"
            ), *choices]
        return SessionConfigOptionSelect(
            id=FieldCodec.encode(type(self)),
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
        choices = await self.choices(thread)
        if value not in {choice.value for choice in choices}:
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
        await Coordination.run_worker(partial(owner.comms.threads.set_thread_model, thread.name, value))


class ThinkingLevelConfigOption(CatalogConfigOption):
    title = "Thinking level"
    description = "Reasoning effort used by this persistent agent thread"
    category = "thought_level"

    def current_value(self, thread: Thread) -> str | None:
        return ThinkingLevel.optional_name(thread.thinking_level)

    async def discover(self, thread: Thread) -> list[SessionConfigSelectOption]:
        if os.environ.get("AGENT_COMMS_AGENT_MODELS"):
            levels = [member.declared_name for member in ThinkingLevel.members_with(ThinkingLevel) if member.ordinary_choice]
        else:
            data = await GetAvailableThinkingLevels().discover(
                self.agent_bin, self.agent_args.with_model(thread.model).argv
            )
            levels = [member.declared_name for member in data.levels] or ["off"]
        return [SessionConfigSelectOption(value=level, name=level.title()) for level in levels]

    async def apply(
        self, owner: ConfigOptions, session_id: str, thread: Thread, value: str
    ) -> None:
        await owner.set_active_backend_option(
            session_id, SetThinkingLevel(level=value), "Thinking level change timed out"
        )
        await Coordination.run_worker(partial(owner.comms.threads.set_thread_thinking_level, thread.name, value))


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
        self.configuration = RestartEnvironment.inherit(os.environ)
        self.catalogs: dict[type[CatalogConfigOption], CatalogConfigOption] = {}
        self.catalog_publish_lock = asyncio.Lock()
        self.session_catalog_generation: dict[str, int] = {}
        self.session_config_signature: dict[str, tuple[tuple[str, str | None], ...]] = {}
        self.setting_requests = PendingRequests()

    def catalog_for(self, member: type[CatalogConfigOption]) -> CatalogConfigOption:
        if member not in self.catalogs:
            self.catalogs[member] = member(self.agent_bin, self.agent_args, self.configuration)
        return self.catalogs[member]

    @property
    def catalog_generation(self) -> int:
        return sum(option.generation for option in self.catalogs.values())

    async def options(self, thread_name: str) -> list[Any]:
        thread = await Coordination.run_worker(partial(self.comms.registry.require, thread_name))
        return [
            await self.catalog_for(member).describe(thread)
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
        thread = await Coordination.run_worker(partial(self.comms.registry.require, thread_name))
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
        thread = await Coordination.run_worker(partial(self.comms.registry.require, name))
        await self.catalog_for(member).change(self, session_id, thread, value)
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
        inbox = await self.effects.turns.active_backend_inbox(session_id)
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
        await self.publish_configuration(session_id, name)

    async def publish_configuration(self, session_id: str, thread_name: str) -> None:
        thread = await Coordination.run_worker(partial(self.comms.registry.require, thread_name))
        signature = self.signature(thread)
        if self.session_config_signature.get(session_id) == signature:
            return
        self.session_config_signature[session_id] = signature
        if self.sessions.client is None and not self.sessions.runtime_enabled:
            return
        await self.publish(session_id, await self.options(thread_name))
