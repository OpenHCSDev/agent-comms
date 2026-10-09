"""Configuration owns its catalogs and the updates it has published."""

from __future__ import annotations

import asyncio
import os
from abc import abstractmethod
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from functools import partial
from typing import TYPE_CHECKING, ClassVar

from acp import RequestError
from acp.schema import (
    ConfigOptionUpdate,
    SessionConfigOptionSelect,
    SessionConfigSelectOption,
    SetSessionConfigOptionResponse,
)

from .pi_vocabulary import ThinkingLevel
from .comms import Comms
from .coordinator import Coordination
from .declared_family import DeclaredFamily
from .field_codec import FieldCodec
from .native_arguments import NativeArguments
from .native_pi import NativePiUnavailable
from .owner_launch import RestartEnvironment
from .runtime import RuntimeServer
from .session_effects import SessionEffects
from .threads import Thread

if TYPE_CHECKING:
    from .pi_commands import SettingCommand
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
    """A selected option owns its catalog, auth revision and acquisition lock."""

    def __init__(self, agent_bin: str, agent_args: NativeArguments, configuration: RestartEnvironment):
        self.agent_bin, self.agent_args = agent_bin, agent_args
        self.configuration = configuration
        self.catalogs: dict[str | None, list[SessionConfigSelectOption]] = {}
        self.auth: tuple[int, int] | None = None
        self.lock = asyncio.Lock()

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
                choices = await self.discover(thread)
                if not choices:
                    raise NativePiUnavailable(f"{self.title} catalog has no available choices")
                self.catalogs[key] = choices
            return self.catalogs[key]

    async def describe(self, thread: Thread) -> SessionConfigOptionSelect:
        choices = await self.choices(thread)
        configured = self.current_value(thread)
        selected = configured if configured is not None else ""
        if configured is None:
            choices = [SessionConfigSelectOption(
                value=selected, name="Not configured"
            ), *choices]
        elif selected not in {choice.value for choice in choices}:
            raise NativePiUnavailable(
                f"Configured {self.title.lower()} {selected!r} is absent from its native catalog"
            )
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
            from .pi_commands import GetAvailableModels

            data = await GetAvailableModels().discover(self.agent_bin, self.agent_args.argv)
            values = [model.display_name for model in data.models if model.provider and model.id]
        return [
            SessionConfigSelectOption(value=value, name=value) for value in dict.fromkeys(values)
        ]

    async def apply(
        self, owner: ConfigOptions, session_id: str, thread: Thread, value: str
    ) -> None:
        from .pi_commands import SetModel

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
            from .pi_commands import GetAvailableThinkingLevels

            data = await GetAvailableThinkingLevels().discover(
                self.agent_bin, self.agent_args.with_model(thread.model).argv
            )
            levels = [member.declared_name for member in data.levels]
        return [SessionConfigSelectOption(value=level, name=level.title()) for level in levels]

    async def apply(
        self, owner: ConfigOptions, session_id: str, thread: Thread, value: str
    ) -> None:
        from .pi_commands import SetThinkingLevel

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
        self.publications: dict[str, ConfigOptionUpdate] = {}

    def catalog_for(self, member: type[CatalogConfigOption]) -> CatalogConfigOption:
        if member not in self.catalogs:
            self.catalogs[member] = member(self.agent_bin, self.agent_args, self.configuration)
        return self.catalogs[member]

    async def options(self, thread: Thread) -> list[SessionConfigOptionSelect]:
        """Describe declared choices and settings from one captured thread."""
        return [
            await self.catalog_for(member).describe(thread)
            for member in ConfigOption.members_with(CatalogConfigOption)
        ]

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
        if await self.runtime.session_update(session_id=session_id, update=update):
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
        await self.catalog_for(member).change(self, session_id, thread, value)
        await self.effects.turns.close_idle_backend(session_id)
        async with self.session_options(name) as options:
            await self._publish(session_id, options)
            return SetSessionConfigOptionResponse(config_options=options)

    async def set_active_backend_option(
        self,
        session_id: str,
        command: SettingCommand,
        timeout_message: str,
    ) -> None:
        try:
            turn = await self.effects.turns.active_native_session(session_id)
            if turn is None:
                return
            async with command.pending_response(turn.native.reader, turn.native.proc.stdin) as future:
                response = await asyncio.wait_for(future, timeout=10)
                if response.success is not True:
                    raise RuntimeError(response.error or command.error_message)
        except asyncio.CancelledError as error:
            if asyncio.current_task().cancelling():
                raise
            raise RequestError.invalid_params({
                "reason": "The native session ended before confirming the configuration change"
            }) from error
        except (TimeoutError, RuntimeError, OSError) as error:
            raise RequestError.invalid_params({"reason": str(error) or timeout_message}) from error

    async def sync_thread(self, session_id: str) -> None:
        name = await self.sessions.sync_identity(session_id)
        await self.effects.turns.goals.sync_goal_execution(session_id, name)
        await self.publish_configuration(session_id, name)

    async def publish_configuration(self, session_id: str, thread_name: str) -> None:
        if self.sessions.client is None and not self.sessions.runtime_enabled:
            return
        async with self.session_options(thread_name) as options:
            await self._publish(session_id, options)
