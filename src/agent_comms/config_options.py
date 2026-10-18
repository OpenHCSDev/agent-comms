"""Configuration owns its catalogs and the updates it has published."""

from __future__ import annotations

import asyncio
import os
from dataclasses import replace
from abc import abstractmethod
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING, ClassVar, Protocol

from acp import RequestError
from acp.schema import (
    ConfigOptionUpdate,
    SessionConfigOptionSelect,
    SessionConfigSelectOption,
    SetSessionConfigOptionResponse,
)

from .pi_vocabulary import ThinkingChoice
from .comms import Comms
from .agent_backend import UseSettings
from .coordinator import Coordination
from .declared_family import DeclaredFamily
from .field_codec import FieldCodec
from .native_arguments import NativeArguments
from .native_pi import NativePiUnavailable
from .launch_runtime import RestartEnvironment
from .threads import Thread

if TYPE_CHECKING:
    from .runtime import RuntimeServer
    from .session_effects import SessionEffects
    from .pi_commands import SettingCommand
    from .session_lifecycle import SessionLifecycle


class InvalidConfigChoice(ValueError):
    """A configured value is not a member of its discovered catalog."""


class CatalogSelection(Protocol):
    """The model and thinking selection read by both draft and running catalogs."""

    @property
    def model(self) -> str | None: ...

    @property
    def thinking_level(self) -> type[ThinkingChoice]: ...


class ConfigOption(DeclaredFamily, affix="ConfigOption"):
    title: ClassVar[str]
    description: ClassVar[str]
    category: ClassVar[str]

    @abstractmethod
    def current_value(self, thread: CatalogSelection) -> str | None: ...

    @abstractmethod
    async def discover(self, thread: CatalogSelection) -> list[SessionConfigSelectOption]: ...

    @abstractmethod
    async def describe(self, thread: CatalogSelection) -> SessionConfigOptionSelect: ...

    @abstractmethod
    async def change(
        self, owner: SessionConfigOptions, thread: Thread, value: str
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

    def cache_key(self, thread: CatalogSelection) -> str | None:
        return thread.model

    async def choices(self, thread: CatalogSelection) -> list[SessionConfigSelectOption]:
        async with self.lock:
            if not self.current_auth:
                self.catalogs.clear()
                self.auth = self.configuration.auth_revision()
            key = self.cache_key(thread)
            if key not in self.catalogs:
                # A successful empty catalogue is setup state, not a failed
                # native process. Keep the existing unset choice reachable so
                # the client can connect a provider before selecting a model.
                self.catalogs[key] = await self.discover(thread)
            return self.catalogs[key]

    unconfigured_value = ""

    async def display_choices(self, thread: CatalogSelection) -> list[SessionConfigSelectOption]:
        choices = await self.choices(thread)
        if self.current_value(thread) is None:
            return [SessionConfigSelectOption(
                value=self.unconfigured_value, name="Not configured"
            ), *choices]
        return choices

    async def describe(self, thread: CatalogSelection) -> SessionConfigOptionSelect:
        choices = await self.display_choices(thread)
        configured = self.current_value(thread)
        selected = configured if configured is not None else self.unconfigured_value
        if configured is not None:
            try:
                self.require_value(selected, choices)
            except InvalidConfigChoice as error:
                raise NativePiUnavailable(str(error)) from error
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
        self, owner: SessionConfigOptions, thread: Thread, value: str
    ) -> None: ...

    async def change(
        self, owner: SessionConfigOptions, thread: Thread, value: str
    ) -> None:
        try:
            await self.validate(thread, value)
        except InvalidConfigChoice as error:
            raise RequestError.invalid_params({"reason": str(error)}) from error
        await self.apply(owner, thread, value)

    def require_value(self, value: str, choices: list[SessionConfigSelectOption]) -> None:
        if value not in {choice.value for choice in choices}:
            valid = ", ".join(choice.value for choice in choices)
            raise InvalidConfigChoice(
                f"{self.title} {value!r} is unavailable; choose one of: {valid}"
            )

    async def validate(self, thread: CatalogSelection, value: str) -> None:
        self.require_value(value, await self.choices(thread))


class ModelConfigOption(CatalogConfigOption):
    title = "Model"
    description = "Model for this thread; a running turn switches at its next model call"
    category = "model"

    def current_value(self, thread: CatalogSelection) -> str | None:
        return thread.model

    def cache_key(self, thread: CatalogSelection) -> None:
        return None

    async def discover(self, thread: CatalogSelection) -> list[SessionConfigSelectOption]:
        values = [
            value.strip()
            for value in os.environ.get("AGENT_COMMS_AGENT_MODELS", "").split(",")
            if value.strip()
        ]
        if not values:
            from .pi_commands import GetAvailableModels

            data = await GetAvailableModels().discover(
                self.agent_bin, self.agent_args.argv, self.configuration.root
            )
            values = [model.display_name for model in data.models if model.provider and model.id]
        return [
            SessionConfigSelectOption(value=value, name=value) for value in dict.fromkeys(values)
        ]

    async def apply(
        self, owner: SessionConfigOptions, thread: Thread, value: str
    ) -> None:
        from .pi_commands import SetModel

        provider, model = value.split("/", 1)
        await owner.configure_thread(
            thread, SetModel(provider=provider, model_id=model),
        )


class ThinkingLevelConfigOption(CatalogConfigOption):
    title = "Thinking level"
    description = "Thinking level for this thread; a running turn switches at its next model call"
    category = "thought_level"

    def current_value(self, thread: CatalogSelection) -> str | None:
        return thread.thinking_level.wire_value()

    async def discover(self, thread: CatalogSelection) -> list[SessionConfigSelectOption]:
        from .pi_commands import GetAvailableThinkingLevels

        data = await GetAvailableThinkingLevels().discover(
            self.agent_bin, self.agent_args.with_model(thread.model).argv, self.configuration.root
        )
        levels = [member.declared_name for member in data.levels]
        return [SessionConfigSelectOption(value=level, name=level.title()) for level in levels]

    async def apply(
        self, owner: SessionConfigOptions, thread: Thread, value: str
    ) -> None:
        from .pi_commands import SetThinkingLevel

        await owner.configure_thread(
            thread, SetThinkingLevel(level=value),
        )


class ConfigOptions:
    """Own one launch's declared model and thinking catalogs."""

    def __init__(
        self,
        agent_bin: str,
        agent_args: NativeArguments,
        configuration: RestartEnvironment,
    ):
        self.agent_bin, self.agent_args = agent_bin, agent_args
        self.configuration = configuration
        self.catalogs: dict[type[CatalogConfigOption], CatalogConfigOption] = {}

    @classmethod
    def configured(cls) -> ConfigOptions:
        """Catalogs of the native binary, arguments and environment this process is configured with."""
        return cls(NativeArguments.configured_binary(), NativeArguments.configured(),
                   RestartEnvironment.inherit(os.environ))

    def catalog_for(self, member: type[CatalogConfigOption]) -> CatalogConfigOption:
        if member not in self.catalogs:
            self.catalogs[member] = member(self.agent_bin, self.agent_args, self.configuration)
        return self.catalogs[member]

    async def options(self, thread: CatalogSelection) -> list[SessionConfigOptionSelect]:
        """Describe declared choices and settings from one captured thread."""
        return [
            await self.catalog_for(member).describe(thread)
            for member in ConfigOption.members_with(CatalogConfigOption)
        ]

    async def validate(self, thread: CatalogSelection) -> None:
        """Require every configured value through its owning catalog."""
        for member in ConfigOption.members_with(CatalogConfigOption):
            option = self.catalog_for(member)
            value = option.current_value(thread)
            if value is not None:
                await option.validate(thread, value)


class SessionConfigOptions(ConfigOptions):
    """Add ACP publication and persistence to the shared catalog owner."""

    def __init__(
        self,
        comms: Comms,
        agent_bin: str,
        agent_args: NativeArguments,
        runtime: RuntimeServer,
        sessions: SessionLifecycle,
        effects: SessionEffects,
    ):
        super().__init__(
            agent_bin,
            agent_args,
            replace(RestartEnvironment.inherit(os.environ), root=str(comms.root)),
        )
        self.comms, self.runtime, self.sessions, self.effects = comms, runtime, sessions, effects
        self.catalog_publish_lock = asyncio.Lock()
        self.publications: dict[str, ConfigOptionUpdate] = {}
        # The stored settings each session's owner last saw, to notice a change.
        # The model and thinking level each running turn currently uses.
        self.settings_in_use: dict[str, UseSettings] = {}

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
        try:
            await self.catalog_for(member).change(self, thread, value)
        except (NativePiUnavailable, InvalidConfigChoice) as error:
            raise RequestError.invalid_params({"reason": str(error)}) from error
        await self.effects.turns.close_idle_backend(session_id, thread)
        await self.use_stored_settings(session_id, name)
        async with self.session_options(name) as options:
            await self._publish(session_id, options)
            return SetSessionConfigOptionResponse(config_options=options)

    async def configure_thread(
        self, thread: Thread, command: SettingCommand,
    ) -> None:
        """Store the model and thinking level Pi itself reports for this change."""
        arguments = self.agent_args.with_model(thread.model).with_thinking(
            thread.thinking_level
        )
        state = await command.configure(
            self.agent_bin, arguments.argv, worktree=Path(thread.worktree), root=self.comms.root,
        )
        model = state.model.display_name
        level = state.thinking_level.wire_value()
        if model is None or level is None:
            raise NativePiUnavailable("Native configuration did not report model and thinking level")
        state.model.require_selection(model)
        await Coordination.run_worker(partial(
            self.comms.threads.set_thread_configuration,
            thread, model=model, thinking_level=state.thinking_level,
        ))

    @staticmethod
    def stored_settings(thread: Thread) -> UseSettings:
        return UseSettings(thread.model, thread.thinking_level)

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
