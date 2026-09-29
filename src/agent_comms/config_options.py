"""Configuration owns its catalogs, generations and setting correlations."""

from __future__ import annotations

import asyncio
import os
from abc import abstractmethod
from dataclasses import dataclass, replace
from pathlib import Path
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
from .child_process import BoundedRun
from .comms import Comms
from .declared_family import DeclaredFamily
from .native_pi import NativePiRpcLaunch, NativePiUnavailable
from .pending_requests import PendingRequests
from .pi_commands import (
    GetAvailableModels,
    GetAvailableThinkingLevels,
    PiCommand,
    SetModel,
    SetThinkingLevel,
)
from .pi_payloads import PiResponseData
from .pi_rpc import PiRpcChannel
from .runtime import RuntimeServer
from .session_effects import SessionEffects
from .threads import Thread

if TYPE_CHECKING:
    from .session_lifecycle import SessionLifecycle


@dataclass(frozen=True, slots=True)
class Model:
    id: str
    name: str
    description: str | None = None


class ConfigOption(DeclaredFamily, affix="ConfigOption"):
    title: ClassVar[str]
    description: ClassVar[str]
    category: ClassVar[str]

    @classmethod
    def select(
        cls, current: str, choices: list[SessionConfigSelectOption]
    ) -> SessionConfigOptionSelect:
        return SessionConfigOptionSelect(
            id=cls.declared_name,
            name=cls.title,
            description=cls.description,
            category=cls.category,
            type="select",
            current_value=current,
            options=choices,
        )

    @classmethod
    @abstractmethod
    def current_value(cls, thread: Thread) -> str | None: ...

    @classmethod
    @abstractmethod
    async def describe(cls, owner: ConfigOptions, thread: Thread) -> SessionConfigOptionSelect: ...

    @classmethod
    @abstractmethod
    async def change(
        cls, owner: ConfigOptions, session_id: str, thread: Thread, value: str
    ) -> None: ...


class ModelConfigOption(ConfigOption):
    title = "Model"
    description = "Model used by this persistent agent thread"
    category = "model"

    @classmethod
    def current_value(cls, thread: Thread) -> str | None:
        return thread.model

    @classmethod
    async def describe(cls, owner: ConfigOptions, thread: Thread) -> SessionConfigOptionSelect:
        models = await owner.models_for(thread.name)
        return cls.select(
            cls.current_value(thread),
            [
                SessionConfigSelectOption(value=m.id, name=m.name, description=m.description)
                for m in models
            ],
        )

    @classmethod
    async def discover(cls, owner: ConfigOptions, selected: str | None) -> list[Model]:
        values = [
            value.strip()
            for value in os.environ.get("AGENT_COMMS_AGENT_MODELS", "").split(",")
            if value.strip()
        ]
        if not values:
            data = await owner.discover(GetAvailableModels(), owner.agent_args)
            values = (
                [model.display_name for model in data.models if model.provider and model.id]
                if data is not None
                else []
            )
        if selected and selected not in values:
            values.insert(0, selected)
        return [Model(value, value) for value in dict.fromkeys(values)]

    @classmethod
    async def change(
        cls, owner: ConfigOptions, session_id: str, thread: Thread, value: str
    ) -> None:
        choices = {model.id for model in await owner.models_for(thread.name)}
        if value not in choices:
            raise RequestError.invalid_params({"reason": f"Unknown model: {value!r}"})
        await owner.set_active_backend_option(
            session_id,
            SetModel(provider=value.split("/", 1)[0], model_id=value.split("/", 1)[1]),
            events.ModelChanged,
            "Model change timed out",
        )
        owner.comms.threads.set_thread_model(thread.name, value)
        await ThinkingLevelConfigOption.selection(owner, replace(thread, model=value))


class ThinkingLevelConfigOption(ConfigOption):
    title = "Thinking level"
    description = "Reasoning effort used by this persistent agent thread"
    category = "thought_level"

    @classmethod
    def current_value(cls, thread: Thread) -> str | None:
        return thread.thinking_level

    @classmethod
    async def discover(cls, owner: ConfigOptions, model: str | None) -> list[str]:
        if os.environ.get("AGENT_COMMS_AGENT_MODELS"):
            return ["off", "minimal", "low", "medium", "high"]
        data = await owner.discover(
            GetAvailableThinkingLevels(), backend.args_for_model(owner.agent_args, model)
        )
        return list(data.levels) if data is not None and data.levels else ["off"]

    @classmethod
    async def selection(cls, owner: ConfigOptions, thread: Thread) -> tuple[str, list[str]]:
        levels = await owner.thinking_levels_for(thread.model)
        selected = cls.current_value(thread)
        if selected not in levels:
            selected = "medium" if "medium" in levels else levels[0]
            owner.comms.threads.set_thread_thinking_level(thread.name, selected)
        return selected, levels

    @classmethod
    async def describe(cls, owner: ConfigOptions, thread: Thread) -> SessionConfigOptionSelect:
        selected, levels = await cls.selection(owner, thread)
        return cls.select(
            selected,
            [SessionConfigSelectOption(value=level, name=level.title()) for level in levels],
        )

    @classmethod
    async def change(
        cls, owner: ConfigOptions, session_id: str, thread: Thread, value: str
    ) -> None:
        levels = await owner.thinking_levels_for(thread.model)
        if value not in levels:
            raise RequestError.invalid_params(
                {"reason": f"Thinking level {value!r} is unavailable for this model"}
            )
        await owner.set_active_backend_option(
            session_id,
            SetThinkingLevel(level=value),
            events.ThinkingChanged,
            "Thinking level change timed out",
        )
        owner.comms.threads.set_thread_thinking_level(thread.name, value)


class ConfigOptions:
    def __init__(
        self,
        comms: Comms,
        agent_bin: str,
        agent_args: list[str],
        runtime: RuntimeServer,
        sessions: SessionLifecycle,
        effects: SessionEffects,
    ):
        self.comms, self.agent_bin, self.agent_args = comms, agent_bin, agent_args
        self.runtime, self.sessions, self.effects = runtime, sessions, effects
        self.model_catalog: list[Model] | None = None
        self.model_catalog_auth: tuple[int, int] | None = None
        self.model_catalog_lock = asyncio.Lock()
        self.catalog_publish_lock = asyncio.Lock()
        self.catalog_generation = 0
        self.session_catalog_generation: dict[str, int] = {}
        self.session_config_signature: dict[str, tuple[tuple[str, str | None], ...]] = {}
        self.setting_requests = PendingRequests()
        self.thinking_catalog: dict[tuple[str | None, tuple[int, int]], list[str]] = {}

    async def discover(self, command: PiCommand, arguments: list[str]) -> PiResponseData | None:
        """Read a native catalog without creating a saved session or sending input."""
        try:
            launch = await asyncio.to_thread(
                NativePiRpcLaunch.managed,
                self.agent_bin,
                (
                    *arguments,
                    "--no-extensions",
                    "--no-skills",
                    "--no-context-files",
                    "--no-session",
                ),
                worktree=Path.cwd(),
            )
            async with BoundedRun.session(
                launch.argv, cwd=launch.cwd, env=launch.env, timeout=10
            ) as child:
                stderr = asyncio.create_task(child.discard_stderr())
                try:
                    channel = PiRpcChannel(child.stdout)
                    response = await channel.request(command, child.stdin)
                    return response.data if response.success is True else None
                finally:
                    stderr.cancel()
                    await asyncio.gather(stderr, return_exceptions=True)
        except (TimeoutError, EOFError, ValueError, OSError, NativePiUnavailable):
            return None

    def ensure_thread_model(self, thread_name: str) -> str | None:
        thread = self.comms.registry.require(thread_name)
        selected = thread.model
        if selected is None:
            selected = self.comms.threads.resolve_thread_model(
                thread.name, backend.configured_model(self.agent_args)
            )
        if selected is not None and selected != thread.model:
            self.comms.threads.set_thread_model(thread.name, selected)
        return selected

    async def models_for(self, thread_name: str) -> list[Model]:
        selected = self.ensure_thread_model(thread_name)
        async with self.model_catalog_lock:
            if self.model_catalog is None or self.model_catalog_auth != backend.auth_revision():
                self.model_catalog = await ModelConfigOption.discover(self, selected)
                self.model_catalog_auth = backend.auth_revision()
                self.catalog_generation += 1
        if selected and all(model.id != selected for model in self.model_catalog):
            return [Model(selected, selected), *self.model_catalog]
        return self.model_catalog

    async def thinking_levels_for(self, model: str | None) -> list[str]:
        key = (model, backend.auth_revision())
        if key not in self.thinking_catalog:
            self.thinking_catalog[key] = await ThinkingLevelConfigOption.discover(self, model)
        return self.thinking_catalog[key]

    async def options(self, thread_name: str) -> list[Any]:
        if self.ensure_thread_model(thread_name) is None:
            return []
        thread = self.comms.registry.require(thread_name)
        return [
            await member.describe(self, thread)
            for member in ConfigOption.members_with(ConfigOption)
        ]

    @staticmethod
    def signature(thread: Thread) -> tuple[tuple[str, str | None], ...]:
        return tuple(
            (member.declared_name, member.current_value(thread))
            for member in ConfigOption.members_with(ConfigOption)
        )

    async def session_options(self, session_id: str, thread_name: str) -> list[Any]:
        options = await self.options(thread_name)
        self.session_catalog_generation[session_id] = self.catalog_generation
        thread = self.comms.registry.require(thread_name)
        self.session_config_signature[session_id] = self.signature(thread)
        return options

    async def refresh_auth_models(self) -> None:
        if self.model_catalog is None:
            return
        if self.model_catalog_auth == backend.auth_revision() and all(
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
            member = ConfigOption.decode(config_id)
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
        await member.change(self, session_id, self.comms.registry.require(name), value)
        await self.effects.turns.close_idle_backend(session_id)
        options = await self.options(name)
        await self.publish(session_id, options)
        return SetSessionConfigOptionResponse(config_options=options)

    async def set_active_backend_option(
        self,
        session_id: str,
        command: PiCommand,
        result_type: type[events.SettingChangeResult],
        timeout_message: str,
    ) -> None:
        inbox = self.effects.turns.active_backend_inbox(session_id)
        if inbox is None:
            return
        request_id = uuid4().hex
        future = self.setting_requests.add(result_type, request_id)
        inbox.put_nowait(replace(command, id=request_id).to_rpc())
        try:
            await asyncio.wait_for(future, timeout=10)
        except (TimeoutError, RuntimeError) as error:
            raise RequestError.invalid_params({"reason": str(error) or timeout_message}) from error
        finally:
            self.setting_requests.discard(result_type, request_id)

    async def sync_thread(self, session_id: str) -> None:
        name = await self.sessions.sync_identity(session_id)
        await self.effects.turns.sync_goal_execution(session_id, name)
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
