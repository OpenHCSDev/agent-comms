from __future__ import annotations

from agent_comms.repo import Checkout

from typing import Self

from agent_comms.catalog_document import CatalogAccess
from agent_comms.channel_targets import ResolvedChannel
from agent_comms.channels import Channel
from agent_comms.cli_support.named import NamedCliCommand
from agent_comms.comms import Comms
from agent_comms.nominal.field_codec import Argument
from agent_comms.nominal.field_codec import TargetBound
from agent_comms.registry_document import RegistrySnapshot
from dataclasses import dataclass
from typing import Annotated


@dataclass(frozen=True, kw_only=True)
class ChannelActivityCliCommand(NamedCliCommand, declared_name="channel-activity"):
    help = "Toggle member activity"
    enabled: Annotated[
        bool, TargetBound(), Argument(flags=("--enabled",))
    ] = False

    @classmethod
    def channel_bindings(cls, comms: Comms, resolved: ResolvedChannel, *, project: Checkout, snapshot: RegistrySnapshot, catalog: CatalogAccess) -> tuple[Self, ...]:
        return (cls(name=resolved.channel.name, enabled=not resolved.channel.any_mode),) if resolved.channel.exact else ()

    def apply(self, ctx: Comms) -> Channel:
        return ctx.channels.set_channel_any_mode(self.name, self.enabled)
