from __future__ import annotations

from dataclasses import dataclass
from typing import Self

from agent_comms.cli_support.named import NamedCliCommand
from agent_comms.comms import Comms
from agent_comms.projects.checkouts import Checkout
from agent_comms.registry.catalog_document import CatalogAccess
from agent_comms.registry.registry_document import RegistrySnapshot, ResolvedChannel


@dataclass(frozen=True, kw_only=True)
class ExactTagCliCommand(NamedCliCommand):
    @classmethod
    def for_tag(cls, name: str) -> Self:
        return cls(name=name)

    @classmethod
    def channel_bindings(cls, comms: Comms, resolved: ResolvedChannel, *, project: Checkout, snapshot: RegistrySnapshot, catalog: CatalogAccess) -> tuple[Self, ...]:
        return (cls.for_tag(resolved.channel.name.removeprefix('#')),) if resolved.channel.exact else ()
