from __future__ import annotations

from dataclasses import dataclass
from typing import Self

from agent_comms.cli_support.named import NamedCliCommand
from agent_comms.cli_verbs.cli_command import TargetActionCommand
from agent_comms.cli_verbs.cli_commands import ViewDeletedResult
from agent_comms.comms import Comms
from agent_comms.projects.checkouts import Checkout
from agent_comms.registry.catalog_document import CatalogAccess
from agent_comms.registry.registry_document import RegistrySnapshot, ResolvedChannel


@dataclass(frozen=True, kw_only=True)
class DeleteViewCliCommand(NamedCliCommand, TargetActionCommand[ViewDeletedResult],
                           declared_name="delete-view"):
    help = "Delete saved view"

    @classmethod
    def channel_bindings(cls, comms: Comms, resolved: ResolvedChannel, *, project: Checkout, snapshot: RegistrySnapshot, catalog: CatalogAccess) -> tuple[Self, ...]:
        return resolved.channel.delete_view_bindings(cls)

    def confirmation(self) -> str:
        return f"Delete saved view #{self.name}? Thread tags and messages are preserved."

    def apply(self, ctx: Comms) -> ViewDeletedResult:
        ctx.channels.delete_saved_view(self.name)
        return ViewDeletedResult(self.name)
