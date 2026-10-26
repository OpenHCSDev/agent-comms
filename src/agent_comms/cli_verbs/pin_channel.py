"""Declaration-owned CLI operation and its required dependencies."""

from __future__ import annotations

from agent_comms.catalog_document import CatalogDocument
from agent_comms.channel_targets import ResolvedChannel
from agent_comms.channels import Channel
from agent_comms.cli_command import MultipleTargets
from agent_comms.cli_verbs.named import NamedCliCommand
from agent_comms.cli_verbs.pin_thread import PinThreadCliCommand
from agent_comms.comms import Comms
from agent_comms.field_codec import Argument
from agent_comms.field_codec import TargetBound
from agent_comms.registry_document import RegistrySnapshot
from dataclasses import dataclass
from typing import Annotated


@dataclass(frozen=True, kw_only=True)
class PinChannelCliCommand(MultipleTargets, NamedCliCommand, declared_name="pin-channel"):
    help = "Set channel pin"
    pinned: Annotated[
        bool, TargetBound(), Argument(flags=("--pinned",))
    ] = False

    @classmethod
    def catalog_declaration(cls):
        return PinThreadCliCommand

    @classmethod
    def channel_bindings(cls, comms: Comms, resolved: ResolvedChannel, *, snapshot: RegistrySnapshot, catalog: CatalogDocument | None):
        return (cls(name=resolved.channel.name, pinned=not resolved.channel.pinned),) if resolved.channel.exact else ()

    def apply(self, ctx: Comms) -> Channel:
        return ctx.channels.set_channel_pinned(self.name, self.pinned)
