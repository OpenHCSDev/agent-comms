"""Configuration owns its model and thinking catalogs."""

import os
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable, Mapping
from contextlib import aclosing
from dataclasses import dataclass, field
from functools import cached_property
from pathlib import Path
from typing import ClassVar

from agent_comms.config_option import (
    CatalogChoice,
    CatalogConfigOption,
    ConfigCatalogSource,
    ConfigOption,
    InvalidConfigChoice,
)
from agent_comms.coordination.errors import Refused
from agent_comms.execution.launch_root import LaunchRoot
from agent_comms.execution.launch_runtime import LaunchRuntime, ThreadLaunch
from agent_comms.execution.native_arguments import ModelProvider, NativeArguments
from agent_comms.execution.pi_commands import (
    GetAvailableModels,
    GetAvailableThinkingLevels,
    SetModel,
    SetThinkingLevel,
)
from agent_comms.execution.pi_vocabulary import ServiceTier
from agent_comms.installer.active_route import CommsRoute, resolve_comms_route
from agent_comms.installer.native_package import VerifiedNativePackage
from agent_comms.messaging.bus import Bus
from agent_comms.messaging.watched import FileSource
from agent_comms.nominal.declared_family import DeclaredFamily
from agent_comms.nominal.field_codec import AcpSessionId, FieldCodec
from agent_comms.registry.registry_document import RegistrySnapshot
from agent_comms.registry.thread_model import CatalogSelection, CliInboxSelection
from agent_comms.registry.thread_settings import (
    ContextEditPolicy,
    ContextEditSetting,
    ModelSetting,
    ServiceTierSetting,
    SettingRequest,
    ThinkingSetting,
)
from agent_comms.registry.threads import (
    ChosenContextEdit,
    ChosenServiceTier,
    ModelChoice,
    NextTurnConfiguration,
    Thread,
)
from agent_comms.transport.owner_requests import RetainedOwnerLaunch
from agent_comms.transport.project_request import ProjectRuntimeRequest


class ConfigurationChanges[Configuration](ABC):
    """The configuration writes an option asks its executing session to make."""

    @abstractmethod
    async def configure_thread(
        self, configuration: Configuration, thread: Thread, command: SettingRequest,
    ) -> None: ...

    @abstractmethod
    async def store_configuration(
        self, thread: Thread, change: NextTurnConfiguration,
    ) -> None: ...

    @abstractmethod
    async def switch_to_cli_inbox(self, thread: Thread) -> None: ...


class SessionConfigOption(CatalogConfigOption, declared_name=None):
    """A catalog-backed session option that also applies changes to a Thread."""

    owner: ConfigOptions

    @abstractmethod
    async def apply(
        self, owner: ConfigurationChanges[ConfigOptions], thread: Thread, value: str
    ) -> None: ...

    async def change(
        self, owner: ConfigurationChanges[ConfigOptions], thread: Thread, value: str
    ) -> None:
        try:
            await self.validate(thread, value)
        except InvalidConfigChoice as error:
            raise Refused.invalid_params({"reason": str(error)}) from error
        await self.apply(owner, thread, value)


class ModelConfigOption(ModelSetting, SessionConfigOption):






    def cache_key(self, thread: CatalogSelection) -> tuple[Path, tuple[str, ...]]:
        return (thread.project_path, self.owner.agent_args.argv)

    async def discover(self, thread: CatalogSelection) -> list[CatalogChoice]:
        return [
            *(await self.owner.models.choices(self.owner, thread.project_path)),
            CatalogChoice(
                name=CliInboxSelection.choice_label,
                value=CliInboxSelection.wire_value,
                description=CliInboxSelection.choice_description,
            ),
        ]

    async def apply(
        self, owner: ConfigurationChanges[ConfigOptions], thread: Thread, value: str
    ) -> None:
        choice = FieldCodec.decode(ModelChoice, value)
        await choice.apply_acp(owner, self.owner, thread, SetModel)


class ThinkingLevelConfigOption(ThinkingSetting, SessionConfigOption):






    async def discover(self, thread: CatalogSelection) -> list[CatalogChoice]:
        if not thread.model.discovers_thinking_levels:
            return []
        declared_levels = (
            thread.model.native(self.owner.agent_args).provider_type.thinking_levels()
        )
        if declared_levels:
            return [CatalogChoice(value=level, name=level.title()) for level in declared_levels]
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
        self, owner: ConfigurationChanges[ConfigOptions], thread: Thread, value: str
    ) -> None:
        await owner.configure_thread(
            self.owner, thread, SetThinkingLevel(level=value),
        )


class ServiceTierConfigOption(ServiceTierSetting, SessionConfigOption):


    # ACP carries only its declared categories and salvages any other to None.




    async def discover(self, thread: CatalogSelection) -> list[CatalogChoice]:
        return [CatalogChoice(name=tier.value.title(), value=tier.value) for tier in ServiceTier]

    async def apply(
        self, owner: ConfigurationChanges[ConfigOptions], thread: Thread, value: str
    ) -> None:
        await owner.store_configuration(
            thread, NextTurnConfiguration(
                service_tier=ChosenServiceTier(FieldCodec.decode(ServiceTier, value)),
            ),
        )


class ContextEditConfigOption(ContextEditSetting, SessionConfigOption):
    async def discover(self, thread: CatalogSelection) -> list[CatalogChoice]:
        return [
            CatalogChoice(name=policy.value.replace("_", " ").title(), value=policy.value)
            for policy in ContextEditPolicy
        ]

    async def apply(
        self, owner: ConfigurationChanges[ConfigOptions], thread: Thread, value: str
    ) -> None:
        await owner.store_configuration(
            thread, NextTurnConfiguration(
                context_edit_policy=ChosenContextEdit(
                    FieldCodec.decode(ContextEditPolicy, value)
                ),
            ),
        )


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
    async def choices(self, owner: ConfigOptions, worktree: Path) -> list[CatalogChoice]:
        data = await GetAvailableModels().discover(
            owner.launch.binary, owner.agent_args.argv, owner.route.root, package=owner.package,
            configuration=owner.launch.configuration, worktree=worktree,
        )
        seen: dict[str, CatalogChoice] = {}
        for model in data.models:
            for text in model.catalog_texts():
                seen.setdefault(
                    text,
                    CatalogChoice(
                        name=model.catalog_label(text),
                        value=text,
                        description=model.catalog_description(),
                    ),
                )
        for provider in ModelProvider.members_with(ModelProvider):
            for model_id, label, description in provider.catalog_models():
                qualified = f"{provider.declared_name}/{model_id}"
                seen.setdefault(
                    qualified,
                    CatalogChoice(
                        name=label,
                        value=qualified,
                        description=description,
                    ),
                )
        return list(seen.values())

    async def qualified(self, owner: ConfigOptions, worktree: Path) -> tuple[str, ...]:
        return tuple(choice.editor_value for choice in await self.choices(owner, worktree))


@dataclass(eq=False)
class ConfigOptions(ConfigCatalogSource):
    """Own one launch's settings and declared model and thinking catalogs."""

    launch: RetainedOwnerLaunch
    route: CommsRoute
    catalogs: dict[type[SessionConfigOption], SessionConfigOption] = field(default_factory=dict, init=False)

    @cached_property
    def agent_args(self) -> NativeArguments:
        return NativeArguments.parse(self.launch.arguments)

    @cached_property
    def models(self) -> ModelCatalog:
        return ModelCatalog.configured(self.launch.environment)

    @cached_property
    def package(self) -> VerifiedNativePackage:
        """The verified artifact, acquired when a catalog or configuration first runs Pi."""
        return self.route.package_for_command(self.launch.binary, LaunchRuntime.running())

    @classmethod
    def configured(cls) -> ConfigOptions:
        """The CLI ingress's catalog; hosted owners supply their original launch directly."""
        return cls.for_route(resolve_comms_route(LaunchRoot.current().path))

    @classmethod
    def for_route(cls, route: CommsRoute) -> ConfigOptions:
        """The catalogs of the Core serving this route, with the owner launch configured here."""
        return cls(RetainedOwnerLaunch.configured(), route)

    async def observe_authentication(
        self, session_id: AcpSessionId, publish: Callable[[AcpSessionId], Awaitable[object]],
    ) -> None:
        """The session retires this configuration's watch with its other producers."""
        async with aclosing(Bus.watch_sources(
            *(FileSource(path) for path in self.launch.configuration.catalog_files),
        )) as observations:
            async for _ in observations:
                await publish(session_id)

    def native_environment(
        self, thread: Thread, snapshot: RegistrySnapshot,
    ) -> dict[str, str]:
        """Project this launch's configuration with the exact registered owner binding."""
        return {
            **ThreadLaunch.strip(dict(self.launch.environment)),
            **self.launch.configuration.encode(),
            **ProjectRuntimeRequest.native_environment(thread, self.route.root, snapshot),
        }

    def catalog_for(self, member: type[CatalogConfigOption]) -> SessionConfigOption:
        resolved = SessionConfigOption.implementing(member)
        if resolved not in self.catalogs:
            self.catalogs[resolved] = resolved(self)
        return self.catalogs[resolved]


    async def validate(self, thread: CatalogSelection) -> None:
        """Require every configured value through its owning catalog."""
        for member in CatalogConfigOption.members_with(CatalogConfigOption):
            option = self.catalog_for(member)
            value = option.current_value(thread)
            if value is not None:
                await option.validate(thread, value)


ConfigOption._catalog_owner = ConfigOptions
