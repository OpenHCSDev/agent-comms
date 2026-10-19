"""Configuration owns its catalogs and the updates it has published."""

from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass, replace
from abc import abstractmethod
from collections.abc import Callable
from contextlib import aclosing
from functools import cached_property, partial
from pathlib import Path
from typing import TYPE_CHECKING, ClassVar, Protocol

from acp import Client, RequestError
from acp.schema import (
    ConfigOptionUpdate,
    SessionConfigOptionSelect,
    SessionConfigOptionBoolean,
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
from .native_pi import NativePiUnavailable, NativePiRpcLaunch
from .native_package import VerifiedNativePackage
from .launch_runtime import LaunchRuntime, RestartEnvironment
from .threads import Thread
from .wire_watch import WatchSource, WireWatch

if TYPE_CHECKING:
    from .runtime import RuntimeServer, SocketClient
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


@dataclass(frozen=True, slots=True)
class CatalogChoice:
    """A catalog value; only the ACP view needs a string for an unset value."""
    name: str
    value: str | None

    @property
    def acp_value(self) -> str:
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
    async def describe(self, thread: CatalogSelection) -> SessionConfigOptionSelect: ...

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
        self.auth: tuple[int, int] | None = None
        self.lock = asyncio.Lock()

    @property
    def current_auth(self) -> bool:
        return self.auth == self.configuration.auth_revision()

    def cache_key(self, thread: CatalogSelection) -> tuple[str, ...]:
        return self.agent_args.with_model(thread.model).argv

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
        return await asyncio.shield(catalog)

    async def display_choices(self, thread: CatalogSelection) -> list[CatalogChoice]:
        choices = await self.choices(thread)
        if self.current_value(thread) is None:
            return [CatalogChoice(name="Not configured", value=None), *choices]
        return choices

    async def describe(self, thread: CatalogSelection) -> SessionConfigOptionSelect:
        choices = await self.display_choices(thread)
        configured = self.current_value(thread)
        try:
            self.require_value(configured, choices)
        except InvalidConfigChoice as error:
            raise NativePiUnavailable(str(error)) from error
        return SessionConfigOptionSelect(
            id=FieldCodec.encode(type(self)),
            name=self.title,
            description=self.description,
            category=self.category,
            type="select",
            current_value=next(choice.acp_value for choice in choices if choice.value == configured),
            options=[SessionConfigSelectOption(name=choice.name, value=choice.acp_value)
                     for choice in choices],
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

    def require_value(self, value: str | None, choices: list[CatalogChoice]) -> None:
        if value not in {choice.value for choice in choices}:
            valid = ", ".join(choice.acp_value for choice in choices)
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
                self.agent_bin, self.agent_args.argv, self.configuration.root, package=self.package
            )
            values = [model.display_name for model in data.models if model.provider and model.id]
        return [
            CatalogChoice(value=value, name=value) for value in dict.fromkeys(values)
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

    async def discover(self, thread: CatalogSelection) -> list[CatalogChoice]:
        from .pi_commands import GetAvailableThinkingLevels

        data = await GetAvailableThinkingLevels().discover(
            self.agent_bin, self.agent_args.with_model(thread.model).argv, self.configuration.root,
            package=self.package,
        )
        levels = [member.declared_name for member in data.levels]
        return [CatalogChoice(value=level, name=level.title()) for level in levels]

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
        acquire_package: Callable[[], VerifiedNativePackage],
    ):
        self.agent_bin, self.agent_args = agent_bin, agent_args
        self.configuration = configuration
        self.acquire_package = acquire_package
        self.catalogs: dict[type[CatalogConfigOption], CatalogConfigOption] = {}

    @cached_property
    def package(self) -> VerifiedNativePackage:
        """The verified artifact, acquired when a catalog or configuration first runs Pi."""
        return self.acquire_package()

    @classmethod
    def configured(cls) -> ConfigOptions:
        """Catalogs of the native binary, arguments and environment this process is configured with."""
        command = NativeArguments.configured_binary()
        configuration = RestartEnvironment.inherit(os.environ)
        package = partial(
            NativePiRpcLaunch.package_for_command,
            command, LaunchRuntime.running(), configuration.root,
        )
        return cls(command, NativeArguments.configured(), configuration, package)

    def catalog_for(self, member: type[CatalogConfigOption]) -> CatalogConfigOption:
        if member not in self.catalogs:
            self.catalogs[member] = member(self.agent_bin, self.agent_args, self.configuration, self.package)
        return self.catalogs[member]

    async def options(
        self, thread: CatalogSelection,
    ) -> list[SessionConfigOptionSelect | SessionConfigOptionBoolean]:
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
            partial(comms.owners.launch_authority.package_for, agent_bin),
        )
        self.comms, self.runtime, self.sessions, self.effects = comms, runtime, sessions, effects
        self.catalog_publish_lock = asyncio.Lock()
        self.publications: dict[str, ConfigOptionUpdate] = {}
        self._publication_sessions: set[str] = set()
        self.observer = self.runtime.background(self._observe_configuration())
        # The stored settings each session's owner last saw, to notice a change.
        # The model and thinking level each running turn currently uses.
        self.settings_in_use: dict[str, UseSettings] = {}

    async def _publish(
        self, session_id: str,
        options: list[SessionConfigOptionSelect | SessionConfigOptionBoolean],
    ) -> None:
        """Publish within the configuration owner's acquisition lifetime."""
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
            member = CatalogConfigOption.member_named(config_id)
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
        return SetSessionConfigOptionResponse(
            config_options=await self.publish_configuration(session_id),
        )

    async def configure_thread(
        self, thread: Thread, command: SettingCommand,
    ) -> None:
        """Store the model and thinking level Pi itself reports for this change."""
        arguments = self.agent_args.with_model(thread.model).with_thinking(
            thread.thinking_level
        )
        state = await command.configure(
            self.agent_bin, arguments.argv, package=self.package, worktree=Path(thread.worktree), root=self.comms.root,
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

    async def apply_stored_settings(self, session_id: str) -> None:
        """Apply execution state without discovering presentation catalogs."""
        name = await self.sessions.sync_identity(session_id)
        await self.effects.turns.goals.sync_goal_execution(session_id, name)
        await self.use_stored_settings(session_id, name)

    async def observe_configuration(self, session_id: str, client: Client | SocketClient | None) -> None:
        """Subscribe an ACP consumer and supply its own initial configuration."""
        if client is None:
            return
        self._publication_sessions.add(session_id)
        self.runtime.background(self.send_menu(client, session_id))

    async def send_menu(self, client: Client | SocketClient, session_id: str) -> None:
        options = await self.options_for_session(session_id)
        async with self.catalog_publish_lock:
            await client.session_update(session_id=session_id, update=ConfigOptionUpdate(
                session_update="config_option_update", config_options=options,
            ))

    async def options_for_session(
        self, session_id: str,
    ) -> list[SessionConfigOptionSelect | SessionConfigOptionBoolean]:
        name = self.sessions.require(session_id)
        thread = await Coordination.run_worker(partial(self.comms.registry.require, name))
        return await self.options(thread)

    def stop_observing_configuration(self, session_id: str) -> None:
        self._publication_sessions.discard(session_id)
        self.publications.pop(session_id, None)

    async def _observe_configuration(self) -> None:
        sources = (
            WatchSource.file(self.comms.registry.store.path),
            WatchSource.file(self.configuration.native_config / "auth.json"),
        )
        async with aclosing(WireWatch.observations(*sources)) as observations:
            async for _ in observations:
                for session_id in tuple(self._publication_sessions):
                    await self.publish_configuration(session_id)

    async def close(self) -> None:
        self.observer.cancel()
        await asyncio.gather(self.observer, return_exceptions=True)
        self._publication_sessions.clear()

    async def publish_configuration(
        self, session_id: str,
    ) -> list[SessionConfigOptionSelect | SessionConfigOptionBoolean]:
        options = await self.options_for_session(session_id)
        async with self.catalog_publish_lock:
            await self._publish(session_id, options)
            return options
