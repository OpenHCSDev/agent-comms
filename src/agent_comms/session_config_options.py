"""ACP session publication consumes the shared configuration catalogs."""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator, Iterable
from contextlib import aclosing, asynccontextmanager
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING

from acp.schema import (ConfigOptionUpdate, SessionConfigOptionSelect,
                        SessionConfigSelectOption, SetSessionConfigOptionResponse)

from .agent_backend import UseSettings
from .bus import Bus
from .child_process import join_retirement
from .config_options import (CatalogConfigOption, CatalogSelection, ConfigOption,
                             ConfigOptions, InvalidConfigChoice)
from .coordination_errors import RequestRefused
from .coordinator import Coordination
from .field_codec import FieldCodec
from .launch_runtime import RestartEnvironment
from .native_failures import NativePiUnavailable
from .threads import ChosenModel, NextTurnConfiguration
from .watched import FileSource

if TYPE_CHECKING:
    from acp.schema import SessionConfigOptionBoolean
    from .comms import Comms
    from .controller import SessionUpdateSink
    from .native_arguments import NativeArguments
    from .pi_commands import SettingCommand
    from .runtime import RuntimeServer
    from .session_effects import SessionEffects
    from .session_lifecycle import SessionLifecycle
    from .threads import Thread


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
            RestartEnvironment.inherit(os.environ),
            comms.route,
        )
        self.comms, self.runtime, self.sessions, self.effects = comms, runtime, sessions, effects
        self.catalog_publish_lock = asyncio.Lock()
        self.publications: dict[str, ConfigOptionUpdate] = {}
        self._publication_sessions: set[str] = set()
        self._menus: set[asyncio.Task[None]] = set()
        self.observer = self.runtime.background(self._observe_configuration())
        # The stored settings each session's owner last saw, to notice a change.
        # The model and thinking level each running turn currently uses.
        self.settings_in_use: dict[str, UseSettings] = {}

    async def describe(self, option: CatalogConfigOption, thread: CatalogSelection) -> SessionConfigOptionSelect:
        choices = await option.display_choices(thread)
        configured = option.current_value(thread)
        try:
            option.require_value(configured, choices)
        except InvalidConfigChoice as error:
            raise NativePiUnavailable(str(error)) from error
        return SessionConfigOptionSelect(
            id=FieldCodec.encode(type(option)), name=option.title,
            description=option.description, category=option.category, type="select",
            current_value=next(choice.editor_value for choice in choices if choice.value == configured),
            options=[SessionConfigSelectOption(name=choice.name, value=choice.editor_value)
                     for choice in choices],
        )

    async def options(self, thread: CatalogSelection) -> list[SessionConfigOptionSelect | SessionConfigOptionBoolean]:
        """Present the shared catalogs through this session's ACP schema."""
        return [await self.describe(self.catalog_for(member), thread)
                for member in ConfigOption.members_with(CatalogConfigOption)]

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
        if await self.runtime.publish_update(session_id=session_id, update=update):
            # Retain exactly what this send supplied, not a later catalog
            # generation or settings observation acquired during its await.
            self.publications[session_id] = update

    async def set_option(
        self, config_id: str, session_id: str, value: str | bool
    ) -> SetSessionConfigOptionResponse:
        try:
            member = CatalogConfigOption.member_named(config_id)
            value = FieldCodec.decode(str, value)
        except ValueError:
            raise RequestRefused.invalid_params(
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
            raise RequestRefused.invalid_params({"reason": str(error)}) from error
        await self.effects.turns.close_idle_backend(session_id, thread)
        await self.use_stored_settings(session_id, name)
        return SetSessionConfigOptionResponse(
            config_options=await self.publish_configuration(session_id),
        )

    async def configure_thread(
        self, thread: Thread, command: SettingCommand,
    ) -> None:
        """Store the model and thinking level Pi itself reports for this change."""
        arguments = thread.model.native(self.agent_args).with_thinking(
            thread.thinking_level
        )
        state = await command.configure(
            self.agent_bin, arguments.argv, package=self.package, worktree=Path(thread.worktree), root=self.comms.root,
        )
        model = state.model.thread_model().require_reported()
        level = state.thinking_level.require_reported(
            NativePiUnavailable("Native configuration did not report a thinking level"))
        await Coordination.run_worker(partial(
            self.comms.threads.set_thread_configuration, thread, NextTurnConfiguration(
                model=ChosenModel(model), thinking_level=level,
            ),
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
        turn = await self.effects.turns.active_backend_turn(session_id)
        if turn.is_open:
            self.settings_in_use[session_id] = settings
            turn.send(settings)

    async def apply_stored_settings(self, session_id: str) -> None:
        """Apply execution state without discovering presentation catalogs."""
        name = await self.sessions.sync_identity(session_id)
        await self.effects.turns.goals.sync_goal_execution(session_id, name)
        await self.use_stored_settings(session_id, name)

    def start_menu(self, session_id: str, client: SessionUpdateSink) -> asyncio.Task[None]:
        """Retain initial publication until it finishes or its consumer retires."""
        self._publication_sessions.add(session_id)
        task = self.runtime.background(self.send_menu(client, session_id))
        self._menus.add(task)
        task.add_done_callback(self._menus.discard)
        return task

    @asynccontextmanager
    async def observe_configuration(
        self, session_id: str, client: SessionUpdateSink,
    ) -> AsyncIterator[None]:
        """An attached socket owns its initial catalog wait through disconnect."""
        task = self.start_menu(session_id, client)
        try:
            yield
        finally:
            await self.retire_observations((task,))

    async def retire_observations(self, tasks: Iterable[asyncio.Task[None]]) -> None:
        pending = tuple(tasks)
        for task in pending:
            task.cancel()
        await join_retirement(asyncio.gather(*pending, return_exceptions=True))

    async def send_menu(self, client: SessionUpdateSink, session_id: str) -> None:
        options = await self.options_for_session(session_id)
        async with self.catalog_publish_lock:
            update = ConfigOptionUpdate(
                session_update="config_option_update", config_options=options,
            )
            if await client.publish_update(session_id=session_id, update=update):
                self.publications[session_id] = update

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
            FileSource(self.comms.registry.store.path),
            FileSource(self.configuration.native_config / "auth.json"),
        )
        async with aclosing(Bus.watch_sources(*sources)) as observations:
            async for _ in observations:
                for session_id in tuple(self._publication_sessions):
                    await self.publish_configuration(session_id)

    async def close(self) -> None:
        await self.retire_observations((*self._menus, self.observer))
        self._publication_sessions.clear()

    async def publish_configuration(
        self, session_id: str,
    ) -> list[SessionConfigOptionSelect | SessionConfigOptionBoolean]:
        options = await self.options_for_session(session_id)
        async with self.catalog_publish_lock:
            await self._publish(session_id, options)
            return options

