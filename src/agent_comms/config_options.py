"""Configuration owns its model and thinking catalogs."""

from __future__ import annotations

import asyncio
import os
from abc import ABC, abstractmethod
from collections import Counter
from collections.abc import Awaitable, Callable, Mapping
from contextlib import aclosing
from dataclasses import dataclass, field
from functools import cached_property, partial
from pathlib import Path
from typing import TYPE_CHECKING, ClassVar

from .coordination_errors import Refused

from .active_route import resolve_comms_route
from .launch_root import LaunchRoot
from .bus import Bus
from .declared_family import DeclaredFamily
from .launch_runtime import LaunchRuntime, ThreadLaunch
from .owner_launch import RetainedOwnerLaunch
from .native_arguments import NativeArguments
from .native_pi import NativePiRpcLaunch
from .pi_vocabulary import ServiceTier, ThinkingChoice
from .coordinator import Coordination
from .field_codec import FieldCodec
from .threads import ChosenServiceTier, NextTurnConfiguration
from .thread_model import CatalogSelection, ThreadModel
from .pi_commands import (
    GetAvailableModels,
    GetAvailableThinkingLevels,
    SetModel,
    SetThinkingLevel,
)
from .watched import FileSource

if TYPE_CHECKING:
    from .field_codec import AcpSessionId
    from .session_config_options import SessionConfigOptions
    from .active_route import CommsRoute
    from .native_package import VerifiedNativePackage
    from .registry_document import RegistrySnapshot
    from .threads import Thread


class InvalidConfigChoice(ValueError):
    """A configured value is not a member of its discovered catalog."""


@dataclass(frozen=True, slots=True)
class RunningSelection(CatalogSelection):
    """What a thread runs: its stored choice, otherwise the model its runtime reports."""
    thread: Thread
    model: ThreadModel = field()

    @property
    def thinking_level(self) -> type[ThinkingChoice]:
        return self.thread.thinking_level

    @property
    def service_tier(self) -> ServiceTier:
        return self.thread.service_tier

    @property
    def project_path(self) -> Path:
        return self.thread.project_path


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

    def __init__(self, owner: ConfigOptions):
        self.owner = owner
        self.catalogs: dict[tuple[Path, tuple[str, ...]], asyncio.Task[list[CatalogChoice]]] = {}
        self.waiters: Counter[asyncio.Task[list[CatalogChoice]]] = Counter()
        self.auth: tuple[int, int] | None = None
        self.lock = asyncio.Lock()

    @property
    def current_auth(self) -> bool:
        return self.auth == self.owner.launch.configuration.auth_revision()

    def cache_key(self, thread: CatalogSelection) -> tuple[Path, tuple[str, ...]]:
        return (thread.project_path, thread.model.native(self.owner.agent_args).argv)

    async def choices(self, thread: CatalogSelection) -> list[CatalogChoice]:
        async with self.lock:
            if not self.current_auth:
                self.catalogs.clear()
                self.auth = self.owner.launch.configuration.auth_revision()
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
            raise Refused.invalid_params({"reason": str(error)}) from error
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

    def cache_key(self, thread: CatalogSelection) -> tuple[Path, tuple[str, ...]]:
        return (thread.project_path, self.owner.agent_args.argv)

    async def discover(self, thread: CatalogSelection) -> list[CatalogChoice]:
        return await self.owner.models.choices(self.owner, thread.project_path)

    async def apply(
        self, owner: SessionConfigOptions, thread: Thread, value: str
    ) -> None:
        provider, model = value.split("/", 1)
        await owner.configure_thread(
            self.owner, thread, SetModel(provider=provider, model_id=model),
        )


class ThinkingLevelConfigOption(CatalogConfigOption):
    title = "Thinking level"
    description = "Thinking level for this thread; a running turn switches at its next model call"
    category = "thought_level"

    def current_value(self, thread: CatalogSelection) -> str | None:
        return thread.thinking_level.wire_value()

    async def discover(self, thread: CatalogSelection) -> list[CatalogChoice]:
        data = await GetAvailableThinkingLevels().discover(
            self.owner.launch.binary,
            thread.model.native(self.owner.agent_args).argv,
            self.owner.route.root,
            package=self.owner.package, configuration=self.owner.launch.configuration,
            worktree=thread.project_path,
        )
        levels = [member.declared_name for member in data.levels]
        return [CatalogChoice(value=level, name=level.title()) for level in levels]

    async def apply(
        self, owner: SessionConfigOptions, thread: Thread, value: str
    ) -> None:
        await owner.configure_thread(
            self.owner, thread, SetThinkingLevel(level=value),
        )


class ServiceTierConfigOption(CatalogConfigOption):
    title = "Service tier"
    description = "Provider request service tier, applied from this thread's next turn"
    category = "service_tier"

    def current_value(self, thread: CatalogSelection) -> str:
        return thread.service_tier.value

    async def discover(self, thread: CatalogSelection) -> list[CatalogChoice]:
        return [CatalogChoice(name=tier.value.title(), value=tier.value) for tier in ServiceTier]

    async def apply(
        self, owner: SessionConfigOptions, thread: Thread, value: str
    ) -> None:
        await Coordination.run_worker(partial(
            owner.comms.threads.set_thread_configuration, thread,
            NextTurnConfiguration(
                service_tier=ChosenServiceTier(FieldCodec.decode(ServiceTier, value)),
            ),
        ))


class ModelCatalog(DeclaredFamily, affix="ModelCatalog"):
    """The launch's selected discovery source, decoded once at its environment boundary."""

    variable: ClassVar[str] = "AGENT_COMMS_AGENT_MODELS"

    @classmethod
    def configured(cls, environment: Mapping[str, str] = os.environ) -> ModelCatalog:
        values = tuple(value.strip() for value in environment.get(cls.variable, "").split(",")
                       if value.strip())
        return SpecifiedModels(values) if values else DiscoveredModels()

    async def choices(self, owner: ConfigOptions, worktree: Path) -> list[CatalogChoice]:
        return [CatalogChoice(value=value, name=value)
                for value in dict.fromkeys(await self.qualified(owner, worktree))]

    @abstractmethod
    async def qualified(self, owner: ConfigOptions, worktree: Path) -> tuple[str, ...]: ...


@dataclass(frozen=True)
class SpecifiedModels(ModelCatalog):
    values: tuple[str, ...]

    async def qualified(self, owner: ConfigOptions, worktree: Path) -> tuple[str, ...]:
        return self.values


@dataclass(frozen=True)
class DiscoveredModels(ModelCatalog):
    async def qualified(self, owner: ConfigOptions, worktree: Path) -> tuple[str, ...]:
        data = await GetAvailableModels().discover(
            owner.launch.binary, owner.agent_args.argv, owner.route.root, package=owner.package,
            configuration=owner.launch.configuration, worktree=worktree,
        )
        return tuple(text for model in data.models if model.provider and model.id
                     for text in model.thread_model().texts())


@dataclass(eq=False)
class ConfigOptions:
    """Own one launch's settings and declared model and thinking catalogs."""

    launch: RetainedOwnerLaunch
    route: CommsRoute
    catalogs: dict[type[CatalogConfigOption], CatalogConfigOption] = field(default_factory=dict, init=False)

    @cached_property
    def agent_args(self) -> NativeArguments:
        return NativeArguments.parse(self.launch.arguments)

    @cached_property
    def models(self) -> ModelCatalog:
        return ModelCatalog.configured(self.launch.environment)

    @cached_property
    def package(self) -> VerifiedNativePackage:
        """The verified artifact, acquired when a catalog or configuration first runs Pi."""
        return NativePiRpcLaunch.package_for_command(
            self.launch.binary, LaunchRuntime.running(), self.route.root,
        )

    @classmethod
    def configured(cls) -> ConfigOptions:
        """The CLI ingress's catalog; hosted owners supply their original launch directly."""
        return cls(RetainedOwnerLaunch.configured(), resolve_comms_route(LaunchRoot.current().path))

    async def observe_authentication(
        self, session_id: AcpSessionId, publish: Callable[[AcpSessionId], Awaitable[object]],
    ) -> None:
        """The session retires this configuration's watch with its other producers."""
        async with aclosing(Bus.watch_sources(FileSource(self.launch.configuration.native_config / "auth.json"))) as observations:
            async for _ in observations:
                await publish(session_id)

    def native_environment(
        self, thread: Thread, snapshot: RegistrySnapshot,
    ) -> dict[str, str]:
        """Project this launch's configuration with the exact registered owner binding."""
        return {
            **ThreadLaunch.strip(dict(self.launch.environment)),
            **self.launch.configuration.encode(),
            **thread.native_environment(self.route.root, snapshot),
        }

    def catalog_for(self, member: type[CatalogConfigOption]) -> CatalogConfigOption:
        if member not in self.catalogs:
            self.catalogs[member] = member(self)
        return self.catalogs[member]


    async def validate(self, thread: CatalogSelection) -> None:
        """Require every configured value through its owning catalog."""
        for member in CatalogConfigOption.members_with(CatalogConfigOption):
            option = self.catalog_for(member)
            value = option.current_value(thread)
            if value is not None:
                await option.validate(thread, value)
