"""Declaration-owned CLI operation and its required dependencies."""

from __future__ import annotations

from typing import Self

from agent_comms.catalog_document import CatalogAccess
from agent_comms.channel_targets import ResolvedChannel
from agent_comms.cli_commands import ViewDeletedResult
from agent_comms.cli_support.named import NamedCliCommand
from agent_comms.comms import Comms
from agent_comms.registry_document import RegistrySnapshot
from dataclasses import dataclass


@dataclass(frozen=True, kw_only=True)
class DeleteViewCliCommand(NamedCliCommand, declared_name="delete-view"):
    help = "Delete saved view"

    @classmethod
    def channel_bindings(cls, comms: Comms, resolved: ResolvedChannel, *, snapshot: RegistrySnapshot, catalog: CatalogAccess) -> tuple[Self, ...]:
        return resolved.channel.delete_view_bindings(cls)

    def confirmation(self) -> str:
        return f"Delete saved view #{self.name}? Thread tags and messages are preserved."

    def apply(self, ctx: Comms) -> ViewDeletedResult:
        ctx.channels.delete_saved_view(self.name)
        return ViewDeletedResult(self.name)
