from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated, Self

from agent_comms.cli_support.named import NamedCliCommand
from agent_comms.cli_verbs.cli_command import CliCommand, MultipleTargets, TargetActionCommand
from agent_comms.cli_verbs.pin_thread import PinThreadCliCommand
from agent_comms.comms import Comms
from agent_comms.messaging.channels import Channel
from agent_comms.nominal.field_codec import Argument, TargetBound
from agent_comms.projects.checkouts import Checkout
from agent_comms.registry.catalog_document import CatalogAccess
from agent_comms.registry.registry_document import RegistrySnapshot, ResolvedChannel


@dataclass(frozen=True, kw_only=True)
class PinChannelCliCommand(MultipleTargets, NamedCliCommand, TargetActionCommand[Channel],
                           declared_name="pin-channel"):
    help = "Set channel pin"
    pinned: Annotated[
        bool, TargetBound(), Argument(flags=("--pinned",))
    ] = False

    @classmethod
    def catalog_declaration(cls) -> type[CliCommand]:
        return PinThreadCliCommand

    @classmethod
    def channel_bindings(cls, comms: Comms, resolved: ResolvedChannel, *, project: Checkout, snapshot: RegistrySnapshot, catalog: CatalogAccess) -> tuple[Self, ...]:
        return (cls(name=resolved.channel.name, pinned=not resolved.channel.pinned),) if resolved.channel.exact else ()

    def apply(self, ctx: Comms) -> Channel:
        return ctx.channels.set_channel_pinned(self.name, self.pinned)
