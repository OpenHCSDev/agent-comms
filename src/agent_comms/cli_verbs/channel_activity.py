from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated, Self

from agent_comms.cli_support.named import NamedCliCommand
from agent_comms.cli_verbs.cli_command import TargetActionCommand
from agent_comms.comms import Comms
from agent_comms.messaging.channels import Channel
from agent_comms.nominal.field_codec import Argument, TargetBound
from agent_comms.projects.checkouts import Checkout
from agent_comms.registry.catalog_document import CatalogAccess
from agent_comms.registry.registry_document import RegistrySnapshot, ResolvedChannel


@dataclass(frozen=True, kw_only=True)
class ChannelActivityCliCommand(NamedCliCommand, TargetActionCommand[Channel],
                                declared_name="channel-activity"):
    help = "Toggle member activity"
    enabled: Annotated[
        bool, TargetBound(), Argument(flags=("--enabled",))
    ] = False

    @classmethod
    def channel_bindings(cls, comms: Comms, resolved: ResolvedChannel, *, project: Checkout, snapshot: RegistrySnapshot, catalog: CatalogAccess) -> tuple[Self, ...]:
        return (cls(name=resolved.channel.name, enabled=not resolved.channel.any_mode),) if resolved.channel.exact else ()

    def apply(self, ctx: Comms) -> Channel:
        return ctx.channels.set_channel_any_mode(self.name, self.enabled)
