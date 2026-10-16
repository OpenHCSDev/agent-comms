"""Native configuration catalogs, selections and changes owned by Core."""

from __future__ import annotations

import asyncio
import os
from abc import abstractmethod
from dataclasses import dataclass
from functools import partial
from pathlib import Path
<<<<<<< HEAD
from typing import ClassVar, Protocol

=======
from typing import TYPE_CHECKING, ClassVar, Protocol

from .pi_vocabulary import ThinkingLevel
>>>>>>> origin/main
from .comms import Comms
from .coordinator import Coordination
from .declared_family import DeclaredFamily
from .launch_runtime import RestartEnvironment
<<<<<<< HEAD
from .native_errors import NativePiUnavailable
from .pi_commands import (
    GetAvailableModels,
    GetAvailableThinkingLevels,
    SetModel,
    SetThinkingLevel,
    SettingCommand,
)
from .pi_vocabulary import ThinkingLevel
from .threads import Thread

=======
from .threads import Thread

if TYPE_CHECKING:
    from acp.schema import ConfigOptionUpdate, SessionConfigOptionSelect, SessionConfigSelectOption, SetSessionConfigOptionResponse
    from .runtime import RuntimeServer
    from .session_effects import SessionEffects
    from .pi_commands import SettingCommand
    from .session_lifecycle import SessionLifecycle

>>>>>>> origin/main

class InvalidConfigChoice(ValueError):
    """A configured value is not a member of its discovered catalog."""


class CatalogSelection(Protocol):
<<<<<<< HEAD
    """The model and thinking selection read by draft and running catalogs."""
=======
    """The model and thinking selection read by both draft and running catalogs."""
>>>>>>> origin/main

    @property
    def model(self) -> str | None: ...

    @property
    def thinking_level(self) -> type[ThinkingLevel] | None: ...


<<<<<<< HEAD
@dataclass(frozen=True)
class ConfigChoice:
    value: str
    name: str
    description: str | None = None


@dataclass(frozen=True)
class CatalogDescription:
    declaration: type[CatalogConfigOption]
    current_value: str | None
    choices: tuple[ConfigChoice, ...]


=======
>>>>>>> origin/main
class ConfigOption(DeclaredFamily, affix="ConfigOption"):
    title: ClassVar[str]
    description: ClassVar[str]
    category: ClassVar[str]

    @abstractmethod
    def current_value(self, thread: CatalogSelection) -> str | None: ...

    @abstractmethod
<<<<<<< HEAD
    async def discover(self, thread: CatalogSelection) -> list[ConfigChoice]: ...

    @abstractmethod
    async def describe(self, thread: CatalogSelection) -> CatalogDescription: ...
=======
    async def discover(self, thread: CatalogSelection) -> list[SessionConfigSelectOption]: ...

    @abstractmethod
    async def describe(self, thread: CatalogSelection) -> SessionConfigOptionSelect: ...
>>>>>>> origin/main

    @abstractmethod
    async def change(self, owner: ConfigOptions, thread: Thread, value: str) -> None: ...


class CatalogConfigOption(ConfigOption):
    """A selected option owns its catalog, auth revision and acquisition lock."""

    def __init__(self, comms: Comms, configuration: RestartEnvironment):
        self.comms = comms
        self.configuration = configuration
        self.catalogs: dict[str | None, list[ConfigChoice]] = {}
        self.auth: tuple[int, int] | None = None
        self.lock = asyncio.Lock()

    @property
    def current_auth(self) -> bool:
        return self.auth == self.configuration.auth_revision()

    def cache_key(self, thread: CatalogSelection) -> str | None:
        return thread.model

<<<<<<< HEAD
    async def choices(self, thread: CatalogSelection) -> list[ConfigChoice]:
=======
    async def choices(self, thread: CatalogSelection) -> list[SessionConfigSelectOption]:
>>>>>>> origin/main
        async with self.lock:
            if not self.current_auth:
                self.catalogs.clear()
                self.auth = self.configuration.auth_revision()
            key = self.cache_key(thread)
            if key not in self.catalogs:
                self.catalogs[key] = await self.discover(thread)
            return self.catalogs[key]

<<<<<<< HEAD
    async def describe(self, thread: CatalogSelection) -> CatalogDescription:
=======
    async def describe(self, thread: CatalogSelection) -> SessionConfigOptionSelect:
        from acp.schema import SessionConfigOptionSelect, SessionConfigSelectOption

>>>>>>> origin/main
        choices = await self.choices(thread)
        configured = self.current_value(thread)
        if configured is not None:
            self.require_value(configured, choices)
        return CatalogDescription(type(self), configured, tuple(choices))

    @abstractmethod
    async def apply(self, owner: ConfigOptions, thread: Thread, value: str) -> None: ...

<<<<<<< HEAD
    async def change(self, owner: ConfigOptions, thread: Thread, value: str) -> None:
        await self.validate(thread, value)
=======
    async def change(
        self, owner: SessionConfigOptions, thread: Thread, value: str
    ) -> None:
        from acp import RequestError

        try:
            await self.validate(thread, value)
        except InvalidConfigChoice as error:
            raise RequestError.invalid_params({"reason": str(error)}) from error
>>>>>>> origin/main
        await self.apply(owner, thread, value)

    def require_value(self, value: str, choices: list[ConfigChoice]) -> None:
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

<<<<<<< HEAD
    async def discover(self, thread: CatalogSelection) -> list[ConfigChoice]:
        data = await GetAvailableModels().discover(
            self.comms, self.comms.settings.arguments,
=======
    async def discover(self, thread: CatalogSelection) -> list[SessionConfigSelectOption]:
        from acp.schema import SessionConfigSelectOption

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
        self, owner: SessionConfigOptions, thread: Thread, value: str
    ) -> None:
        from .pi_commands import SetModel

        provider, model = value.split("/", 1)
        await owner.configure_thread(
            thread, SetModel(provider=provider, model_id=model),
>>>>>>> origin/main
        )
        values = [model.display_name for model in data.models if model.provider and model.id]
        return [ConfigChoice(value=value, name=value) for value in dict.fromkeys(values)]

    async def apply(self, owner: ConfigOptions, thread: Thread, value: str) -> None:
        provider, model = value.split("/", 1)
        await owner.configure_thread(thread, SetModel(provider=provider, model_id=model))


class ThinkingLevelConfigOption(CatalogConfigOption):
    title = "Thinking level"
    description = "Thinking level for this thread; a running turn switches at its next model call"
    category = "thought_level"

    def current_value(self, thread: CatalogSelection) -> str | None:
        return ThinkingLevel.optional_name(thread.thinking_level)

<<<<<<< HEAD
    async def discover(self, thread: CatalogSelection) -> list[ConfigChoice]:
=======
    async def discover(self, thread: CatalogSelection) -> list[SessionConfigSelectOption]:
        from acp.schema import SessionConfigSelectOption
        from .pi_commands import GetAvailableThinkingLevels

>>>>>>> origin/main
        data = await GetAvailableThinkingLevels().discover(
            self.comms, self.comms.settings.arguments.with_model(thread.model)
        )
        return [ConfigChoice(value=member.declared_name, name=member.declared_name.title())
                for member in data.levels]

    async def apply(self, owner: ConfigOptions, thread: Thread, value: str) -> None:
        await owner.configure_thread(thread, SetThinkingLevel(level=value))


class ConfigOptions:
    """Own one launch's declared model/thinking catalogs and native changes."""

    def __init__(self, comms: Comms):
        self.comms = comms
        self.configuration = RestartEnvironment.inherit(os.environ)
        self.catalogs: dict[type[CatalogConfigOption], CatalogConfigOption] = {}

    @classmethod
    def configured(cls) -> ConfigOptions:
        """Catalogs of the native binary, arguments and environment this process is configured with."""
        return cls(NativeArguments.configured_binary(), NativeArguments.configured(),
                   RestartEnvironment.inherit(os.environ))

    def catalog_for(self, member: type[CatalogConfigOption]) -> CatalogConfigOption:
        if member not in self.catalogs:
            self.catalogs[member] = member(self.comms, self.configuration)
        return self.catalogs[member]

<<<<<<< HEAD
    async def descriptions(self, thread: CatalogSelection) -> list[CatalogDescription]:
        return [await self.catalog_for(member).describe(thread)
                for member in ConfigOption.members_with(CatalogConfigOption)]

    async def validate(self, thread: CatalogSelection) -> None:
=======
    async def options(self, thread: CatalogSelection) -> list[SessionConfigOptionSelect]:
        """Describe declared choices and settings from one captured thread."""
        return [
            await self.catalog_for(member).describe(thread)
            for member in ConfigOption.members_with(CatalogConfigOption)
        ]

    async def validate(self, thread: CatalogSelection) -> None:
        """Require every configured value through its owning catalog."""
>>>>>>> origin/main
        for member in ConfigOption.members_with(CatalogConfigOption):
            option = self.catalog_for(member)
            value = option.current_value(thread)
            if value is not None:
                await option.validate(thread, value)

<<<<<<< HEAD
    async def configure_thread(self, thread: Thread, command: SettingCommand) -> None:
        """Store the model and thinking level Pi reports for this change."""
        arguments = self.comms.settings.arguments.with_model(thread.model).with_thinking(
=======

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
        super().__init__(agent_bin, agent_args, RestartEnvironment.inherit(os.environ))
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
        from acp.schema import ConfigOptionUpdate

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
        from acp import RequestError
        from acp.schema import SetSessionConfigOptionResponse

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
>>>>>>> origin/main
            ThinkingLevel.optional_name(thread.thinking_level)
        )
        state = await command.configure(
            self.comms, arguments, worktree=Path(thread.worktree),
        )
        model = state.model.display_name
        level = ThinkingLevel.optional_name(state.thinking_level)
        if model is None or level is None:
            raise NativePiUnavailable("Native configuration did not report model and thinking level")
        state.model.require_selection(model)
        await Coordination.run_worker(partial(
            self.comms.threads.set_thread_configuration,
            thread, model=model, thinking_level=level,
        ))
