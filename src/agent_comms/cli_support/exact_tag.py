from __future__ import annotations

from agent_comms.catalog_document import CatalogAccess
from agent_comms.channel_targets import ResolvedChannel
from agent_comms.cli_support.named import NamedCliCommand
from agent_comms.comms import Comms
from agent_comms.registry_document import RegistrySnapshot
from agent_comms.repo import Checkout
from dataclasses import dataclass
from typing import Self


@dataclass(frozen=True, kw_only=True)
class ExactTagCliCommand(NamedCliCommand):
    @classmethod
    def for_tag(cls, name: str) -> Self:
        return cls(name=name)

    @classmethod
    def channel_bindings(cls, comms: Comms, resolved: ResolvedChannel, *, project: Checkout, snapshot: RegistrySnapshot, catalog: CatalogAccess) -> tuple[Self, ...]:
        return (cls.for_tag(resolved.channel.name.removeprefix('#')),) if resolved.channel.exact else ()
