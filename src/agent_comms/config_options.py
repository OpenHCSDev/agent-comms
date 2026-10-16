"""Native configuration catalogs, selections and changes owned by Core."""

from __future__ import annotations

import asyncio
import os
from abc import abstractmethod
from dataclasses import dataclass
from functools import partial
from pathlib import Path
from typing import ClassVar, Protocol

from .comms import Comms
from .coordinator import Coordination
from .declared_family import DeclaredFamily
from .launch_runtime import RestartEnvironment
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


class InvalidConfigChoice(ValueError):
    """A configured value is not a member of its discovered catalog."""


class CatalogSelection(Protocol):
    """The model and thinking selection read by draft and running catalogs."""

    @property
    def model(self) -> str | None: ...

    @property
    def thinking_level(self) -> type[ThinkingLevel] | None: ...


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


class ConfigOption(DeclaredFamily, affix="ConfigOption"):
    title: ClassVar[str]
    description: ClassVar[str]
    category: ClassVar[str]

    @abstractmethod
    def current_value(self, thread: CatalogSelection) -> str | None: ...

    @abstractmethod
    async def discover(self, thread: CatalogSelection) -> list[ConfigChoice]: ...

    @abstractmethod
    async def describe(self, thread: CatalogSelection) -> CatalogDescription: ...

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

    async def choices(self, thread: CatalogSelection) -> list[ConfigChoice]:
        async with self.lock:
            if not self.current_auth:
                self.catalogs.clear()
                self.auth = self.configuration.auth_revision()
            key = self.cache_key(thread)
            if key not in self.catalogs:
                self.catalogs[key] = await self.discover(thread)
            return self.catalogs[key]

    async def describe(self, thread: CatalogSelection) -> CatalogDescription:
        choices = await self.choices(thread)
        configured = self.current_value(thread)
        if configured is not None:
            self.require_value(configured, choices)
        return CatalogDescription(type(self), configured, tuple(choices))

    @abstractmethod
    async def apply(self, owner: ConfigOptions, thread: Thread, value: str) -> None: ...

    async def change(self, owner: ConfigOptions, thread: Thread, value: str) -> None:
        await self.validate(thread, value)
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

    async def discover(self, thread: CatalogSelection) -> list[ConfigChoice]:
        data = await GetAvailableModels().discover(
            self.comms, self.comms.settings.arguments,
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

    async def discover(self, thread: CatalogSelection) -> list[ConfigChoice]:
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

    def catalog_for(self, member: type[CatalogConfigOption]) -> CatalogConfigOption:
        if member not in self.catalogs:
            self.catalogs[member] = member(self.comms, self.configuration)
        return self.catalogs[member]

    async def descriptions(self, thread: CatalogSelection) -> list[CatalogDescription]:
        return [await self.catalog_for(member).describe(thread)
                for member in ConfigOption.members_with(CatalogConfigOption)]

    async def validate(self, thread: CatalogSelection) -> None:
        for member in ConfigOption.members_with(CatalogConfigOption):
            option = self.catalog_for(member)
            value = option.current_value(thread)
            if value is not None:
                await option.validate(thread, value)

    async def configure_thread(self, thread: Thread, command: SettingCommand) -> None:
        """Store the model and thinking level Pi reports for this change."""
        arguments = self.comms.settings.arguments.with_model(thread.model).with_thinking(
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
