"""Lightweight configuration option contracts; concrete Pi RPC discovery lives in config_options."""

import asyncio
from abc import ABC, abstractmethod
from collections import Counter
from dataclasses import dataclass
from functools import cached_property
from pathlib import Path
from typing import ClassVar, Self

from metaclass_registry.discovery import import_module_preserving_root_logging

from agent_comms.installer.active_route import CommsRoute
from agent_comms.nominal.declared_family import DeclaredFamily
from agent_comms.nominal.field_codec import Description, FieldCodec, FieldPlan
from agent_comms.registry.thread_model import CatalogSelection
from agent_comms.registry.thread_settings import (
    ConfigurationSlot,
    ThreadSettings,
)


class InvalidConfigChoice(ValueError):
    """A configured value is not a member of its discovered catalog."""


@dataclass(frozen=True, slots=True)
class CatalogChoice:
    """A catalog value; only the ACP view needs a string for an unset value."""
    name: str
    value: str | None
    description: str | None = None

    @property
    def editor_value(self) -> str:
        return self.value if self.value is not None else ""


class ConfigCatalogSource(ABC):
    """Configuration catalog owner contract implemented by ConfigOptions."""

    route: CommsRoute

    @classmethod
    @abstractmethod
    def for_route(cls, route: CommsRoute) -> Self: ...

    @abstractmethod
    def catalog_for(self, member: type[CatalogConfigOption]) -> CatalogConfigOption: ...

    @abstractmethod
    async def validate(self, thread: CatalogSelection) -> None: ...


class ConfigOption(DeclaredFamily, affix="ConfigOption"):
    category: ClassVar[str]
    _catalog_owner: ClassVar[type[ConfigCatalogSource]]

    @classmethod
    def _member_declarations(cls) -> tuple[type[Self], ...]:
        import_module_preserving_root_logging("agent_comms.config_options")
        return super()._member_declarations()

    @classmethod
    def catalogs_for(cls, route: CommsRoute) -> ConfigCatalogSource:
        cls._member_declarations()
        return cls._catalog_owner.for_route(route)

    @classmethod
    def declared_field(cls) -> FieldPlan:
        selected, = (plan for plan in FieldCodec.field_plans(ThreadSettings)
                     if any(issubclass(cls, slot.value)
                            for slot in ConfigurationSlot.single(plan.annotation)))
        return selected

    @property
    def title(self) -> str:
        return self.declared_field().label

    @property
    def description(self) -> str:
        return self.declared_field().setting(Description, "")

    def current_value(self, thread: CatalogSelection) -> str | None:
        plan = self.declared_field()
        return FieldCodec.decode(str | None, plan.value.encode(plan.current(thread)))

    @abstractmethod
    async def discover(self, thread: CatalogSelection) -> list[CatalogChoice]: ...


class CatalogConfigOption(ConfigOption):
    """A selected option owns its catalog, auth revision and acquisition lock."""

    @classmethod
    def implementing(cls, role: type) -> type[Self]:
        member, = (member for member in cls.members_with(cls) if issubclass(member, role))
        return member

    def __init__(self, owner: ConfigCatalogSource):
        self.owner = owner
        self.catalogs: dict[tuple[Path, tuple[str, ...]], asyncio.Task[list[CatalogChoice]]] = {}
        self.waiters: Counter[asyncio.Task[list[CatalogChoice]]] = Counter()
        self.auth: tuple[tuple[int, int], ...] | None = None
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
        succeeded = False
        cancelled = False
        try:
            result = await asyncio.shield(catalog)
            succeeded = True
            return result
        except asyncio.CancelledError:
            cancelled = True
            # The shield keeps one waiter's cancellation from failing the others;
            # the last waiter to leave retires the discovery and reaps its child.
            if self.waiters[catalog] == 1:
                catalog.cancel()
                if self.catalogs.get(key) is catalog:
                    del self.catalogs[key]
                await asyncio.gather(catalog, return_exceptions=True)
            raise
        finally:
            if not succeeded and not cancelled and self.catalogs.get(key) is catalog:
                # A failed discovery is not a catalog to keep for the next form.
                del self.catalogs[key]
            self.waiters.subtract((catalog,))
            if not self.waiters[catalog]:
                del self.waiters[catalog]

    async def display_choices(self, thread: CatalogSelection, *, current: str | None) -> list[CatalogChoice]:
        """Keep the editor's current value, distinct from its discovery context, reachable."""
        choices = await self.choices(thread)
        if current in {choice.value for choice in choices}:
            return choices
        name = "Not configured" if current is None else f"{current} (unavailable)"
        return [CatalogChoice(name=name, value=current), *choices]

    def require_value(self, value: str | None, choices: list[CatalogChoice]) -> None:
        if value not in {choice.value for choice in choices}:
            valid = ", ".join(choice.editor_value for choice in choices)
            raise InvalidConfigChoice(
                f"{self.title} {value!r} is unavailable; choose one of: {valid}"
            )

    async def validate(self, thread: CatalogSelection, value: str) -> None:
        self.require_value(value, await self.choices(thread))


class ConfigurationCatalogs:
    """A serving root retains the catalogs and their existing authentication invalidation."""

    route: CommsRoute

    @cached_property
    def configuration_catalogs(self) -> ConfigCatalogSource:
        return ConfigOption.catalogs_for(self.route)
