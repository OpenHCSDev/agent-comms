from __future__ import annotations

from dataclasses import dataclass
from typing import Self

from agent_comms.cli_verbs.cli_verb import CliBus
from agent_comms.cli_support.named import NamedCliCommand
from agent_comms.cli_verbs.cli_command import TargetCommand
from agent_comms.projects.checkouts import Checkout
from agent_comms.registry.catalog_document import CatalogAccess
from agent_comms.registry.registry_document import RegistrySnapshot, ResolvedChannel


@dataclass(frozen=True, kw_only=True)
class ExactTagCliCommand(TargetCommand, NamedCliCommand):
    @classmethod
    def for_tag(cls, name: str) -> Self:
        return cls(name=name)

    @classmethod
    def channel_bindings(cls, comms: CliBus, resolved: ResolvedChannel, *, project: Checkout, snapshot: RegistrySnapshot, catalog: CatalogAccess) -> tuple[Self, ...]:
        return (cls.for_tag(resolved.channel.name.removeprefix('#')),) if resolved.channel.exact else ()
