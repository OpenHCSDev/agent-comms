"""Configure heartbeat publication without resigning as teammate."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated

from agent_comms.cli_verbs.cli_command import CliCommand
from agent_comms.cli_verbs.cli_commands import NameArgument
from agent_comms.cli_verbs.cli_result import CliResult
from agent_comms.comms import Comms
from agent_comms.nominal.field_codec import Argument
from agent_comms.registry.heartbeat_delivery import HeartbeatDelivery


@dataclass(frozen=True)
class HeartbeatDeliveryConfiguredResult(CliResult):
    thread: str
    delivery: type[HeartbeatDelivery]


@dataclass(frozen=True, kw_only=True)
class HeartbeatDeliveryCliCommand(NameArgument, CliCommand, declared_name="heartbeat-delivery"):
    help = "Set heartbeat bus messages on/off; teammate membership and HUD stay unchanged"
    delivery: Annotated[type[HeartbeatDelivery], Argument(flags=("delivery",))]

    def apply(self, ctx: Comms) -> CliResult:
        updated = ctx.threads.set_heartbeat_delivery(self.name, self.delivery)
        return HeartbeatDeliveryConfiguredResult(updated.name, updated.heartbeat_delivery)
