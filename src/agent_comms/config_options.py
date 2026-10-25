"""Configuration owns its model and thinking catalogs."""

from __future__ import annotations

import asyncio
import os
from abc import abstractmethod
from collections import Counter
from dataclasses import dataclass
from functools import cached_property
from typing import TYPE_CHECKING, ClassVar, Protocol

from acp.exceptions import RequestError

from .active_route import resolve_comms_route
from .declared_family import DeclaredFamily
from .launch_root import LaunchRoot
from .launch_runtime import LaunchRuntime, RestartEnvironment
from .native_arguments import ConfiguredBinary, InheritedArguments
from .native_pi import NativePiRpcLaunch
from .pi_commands import (
    GetAvailableThinkingLevels,
    SetModel,
    SetThinkingLevel,
)

if TYPE_CHECKING:
    from .session_config_options import SessionConfigOptions

    from .active_route import CommsRoute
    from .native_arguments import NativeArguments
    from .native_package import VerifiedNativePackage
    from .pi_vocabulary import ThinkingChoice
    from .thread_model import ThreadModel
    from .threads import Thread


class InvalidConfigChoice(ValueError):
    """A configured value is not a member of its discovered catalog."""


class CatalogSelection(Protocol):
    """The model and thinking selection read by both draft and running catalogs."""

    @property
    def model(self) -> ThreadModel: ...

    @property
    def thinking_level(self) -> type[ThinkingChoice]: ...


@dataclass(frozen=True, slots=True)
class RunningSelection:
    """What a thread runs: its stored choice, otherwise the model its runtime reports."""
    model: ThreadModel
    thinking_level: type[ThinkingChoice]


@dataclass(frozen=True, slots=True)
class CatalogChoice:
    """A catalog value; only the ACP view needs a string for an unset value."""
    name: str
    value: str | None

    @property
    def editor_value(self) -> str:
        return self.value if self.value is not None else ""


class ConfigOption(DeclaredFamily, affix="ConfigOption"):
    title: ClassVar[str]
    description: ClassVar[str]
    category: ClassVar[str]

    @abstractmethod
    def current_value(self, thread: CatalogSelection) -> str | None: ...

    @abstractmethod
    async def discover(self, thread: CatalogSelection) -> list[CatalogChoice]: ...


    @abstractmethod
    async def change(
        self, owner: SessionConfigOptions, thread: Thread, value: str
    ) -> None: ...


class CatalogConfigOption(ConfigOption):
    """A selected option owns its catalog, auth revision and acquisition lock."""

    def __init__(self, agent_bin: str, agent_args: NativeArguments, configuration: RestartEnvironment,
                 package: VerifiedNativePackage):
        self.agent_bin, self.agent_args = agent_bin, agent_args
        self.configuration = configuration
        self.package = package
        self.catalogs: dict[tuple[str, ...], asyncio.Task[list[CatalogChoice]]] = {}
        self.waiters: Counter[asyncio.Task[list[CatalogChoice]]] = Counter()
        self.auth: tuple[int, int] | None = None
        self.lock = asyncio.Lock()

    @property
    def current_auth(self) -> bool:
        return self.auth == self.configuration.auth_revision()

    def cache_key(self, thread: CatalogSelection) -> tuple[str, ...]:
        return thread.model.native(self.agent_args).argv

    async def choices(self, thread: CatalogSelection) -> list[CatalogChoice]:
        async with self.lock:
            if not self.current_auth:
                self.catalogs.clear()
                self.auth = self.configuration.auth_revision()
            key = self.cache_key(thread)
            if key not in self.catalogs:
                # A successful empty catalogue is setup state, not a failed
                # native process. Keep the existing unset choice reachable so
                # the client can connect a provider before selecting a model.
                self.catalogs[key] = asyncio.create_task(self.discover(thread))
            catalog = self.catalogs[key]
            self.waiters[catalog] += 1
        try:
            return await asyncio.shield(catalog)
        except asyncio.CancelledError:
            # The shield keeps one waiter's cancellation from failing the others;
            # the last waiter to leave retires the discovery and reaps its child.
            if self.waiters[catalog] == 1:
                catalog.cancel()
                if self.catalogs.get(key) is catalog:
                    del self.catalogs[key]
                await asyncio.gather(catalog, return_exceptions=True)
            raise
        finally:
            self.waiters.subtract((catalog,))
            if not self.waiters[catalog]:
                del self.waiters[catalog]

    async def display_choices(self, thread: CatalogSelection) -> list[CatalogChoice]:
        choices = await self.choices(thread)
        if self.current_value(thread) is None:
            return [CatalogChoice(name="Not configured", value=None), *choices]
        return choices


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

    def require_value(self, value: str | None, choices: list[CatalogChoice]) -> None:
        if value not in {choice.value for choice in choices}:
            valid = ", ".join(choice.editor_value for choice in choices)
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
        return thread.model.otherwise(None)

    def cache_key(self, thread: CatalogSelection) -> tuple[str, ...]:
        return self.agent_args.argv

    async def discover(self, thread: CatalogSelection) -> list[CatalogChoice]:
        values = [
            value.strip()
            for value in os.environ.get("AGENT_COMMS_AGENT_MODELS", "").split(",")
            if value.strip()
        ]
        if not values:
            from .pi_commands import GetAvailableModels

            data = await GetAvailableModels().discover(
                self.agent_bin,
                self.agent_args.argv,
                LaunchRoot.current().path,
                package=self.package,
            )
            values = [
                text for model in data.models if model.provider and model.id
                for text in model.thread_model().texts()
            ]
        return [
            CatalogChoice(value=value, name=value) for value in dict.fromkeys(values)
        ]

    async def apply(
        self, owner: SessionConfigOptions, thread: Thread, value: str
    ) -> None:
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

    async def discover(self, thread: CatalogSelection) -> list[CatalogChoice]:
        data = await GetAvailableThinkingLevels().discover(
            self.agent_bin,
            thread.model.native(self.agent_args).argv,
            LaunchRoot.current().path,
            package=self.package,
        )
        levels = [member.declared_name for member in data.levels]
        return [CatalogChoice(value=level, name=level.title()) for level in levels]

    async def apply(
        self, owner: SessionConfigOptions, thread: Thread, value: str
    ) -> None:
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
        route: CommsRoute,
    ):
        self.agent_bin, self.agent_args = agent_bin, agent_args
        self.configuration = configuration
        self.route = route
        self.catalogs: dict[type[CatalogConfigOption], CatalogConfigOption] = {}

    @cached_property
    def package(self) -> VerifiedNativePackage:
        """The verified artifact, acquired when a catalog or configuration first runs Pi."""
        return NativePiRpcLaunch.package_for_command(
            self.agent_bin, LaunchRuntime.running(), self.route.root,
        )

    @classmethod
    def configured(cls) -> ConfigOptions:
        """Catalogs of the native binary, arguments and environment this process is configured with."""
        command = ConfiguredBinary().configured()
        configuration = RestartEnvironment.inherit(os.environ)
        return cls(
            command, InheritedArguments().configured(), configuration,
            resolve_comms_route(LaunchRoot.current().path),
        )

    def catalog_for(self, member: type[CatalogConfigOption]) -> CatalogConfigOption:
        if member not in self.catalogs:
            self.catalogs[member] = member(self.agent_bin, self.agent_args, self.configuration, self.package)
        return self.catalogs[member]


    async def validate(self, thread: CatalogSelection) -> None:
        """Require every configured value through its owning catalog."""
        for member in ConfigOption.members_with(CatalogConfigOption):
            option = self.catalog_for(member)
            value = option.current_value(thread)
            if value is not None:
                await option.validate(thread, value)
