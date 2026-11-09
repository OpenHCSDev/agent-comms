from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated, Self

from agent_comms.cli_support.named import NamedCliCommand
from agent_comms.cli_verbs.archive import ArchiveCliCommand
from agent_comms.cli_verbs.cli_command import CliCommand, MultipleTargets, TargetActionCommand
from agent_comms.comms import Comms
from agent_comms.messaging.channels import Channel
from agent_comms.nominal.field_codec import Argument, TargetBound
from agent_comms.projects.checkouts import Checkout
from agent_comms.registry.catalog_document import CatalogAccess
from agent_comms.registry.registry_document import RegistrySnapshot, ResolvedChannel


@dataclass(frozen=True, kw_only=True)
class ArchiveChannelCliCommand(MultipleTargets, NamedCliCommand, TargetActionCommand[Channel],
                               declared_name="archive-channel"):
    help = "Archive channel"
    archived: Annotated[
        bool, TargetBound(), Argument(flags=("--archived",))
    ] = True

    @classmethod
    def catalog_declaration(cls) -> type[CliCommand]:
        return ArchiveCliCommand

    @classmethod
    def channel_bindings(cls, comms: Comms, resolved: ResolvedChannel, *, project: Checkout, snapshot: RegistrySnapshot, catalog: CatalogAccess) -> tuple[Self, ...]:
        return (cls(name=resolved.channel.name),) if resolved.channel.can_set_archived(cls.archived) else ()

    def confirmation(self) -> str:
        return (f"Archive {self.name}? Hide the channel without removing threads, tags or history. It can be restored."
                if self.archived else '')

    def apply(self, ctx: Comms) -> Channel:
        return ctx.channels.set_channel_archived(self.name, self.archived)
