"""Declaration-owned CLI operation and its required dependencies."""

from __future__ import annotations

from agent_comms.catalog_document import CatalogAccess, NoCatalog
from agent_comms.channel_targets import ResolvedChannel
from agent_comms.channels import Channel
from agent_comms.cli_command import MultipleTargets
from agent_comms.cli_verbs.archive import ArchiveCliCommand
from agent_comms.cli_support.named import NamedCliCommand
from agent_comms.comms import Comms
from agent_comms.field_codec import Argument
from agent_comms.field_codec import TargetBound
from agent_comms.registry_document import RegistrySnapshot
from dataclasses import dataclass
from typing import Annotated


@dataclass(frozen=True, kw_only=True)
class ArchiveChannelCliCommand(MultipleTargets, NamedCliCommand, declared_name="archive-channel"):
    help = "Archive channel"
    archived: Annotated[
        bool, TargetBound(), Argument(flags=("--archived",))
    ] = True

    @classmethod
    def catalog_declaration(cls):
        return ArchiveCliCommand

    @classmethod
    def channel_bindings(cls, comms: Comms, resolved: ResolvedChannel, *, snapshot: RegistrySnapshot, catalog: CatalogAccess):
        return (cls(name=resolved.channel.name),) if resolved.channel.can_set_archived(cls.archived) else ()

    def confirmation(self):
        return (f"Archive {self.name}? Hide the channel without removing threads, tags or history. It can be restored."
                if self.archived else '')

    def apply(self, ctx: Comms) -> Channel:
        return ctx.channels.set_channel_archived(self.name, self.archived)
