"""Configure heartbeat inputs without resigning as commander."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated

from agent_comms.cli_command import CliCommand
from agent_comms.cli_commands import NameArgument
from agent_comms.cli_result import CliResult
from agent_comms.comms import Comms
from agent_comms.heartbeat_delivery import HeartbeatDelivery
from agent_comms.nominal.field_codec import Argument


@dataclass(frozen=True)
class HeartbeatDeliveryConfiguredResult(CliResult):
    thread: str
    delivery: type[HeartbeatDelivery]


@dataclass(frozen=True, kw_only=True)
class HeartbeatDeliveryCliCommand(NameArgument, CliCommand, declared_name="heartbeat-delivery"):
    help = "Set automatic heartbeat input on/off; commander membership and HUD stay unchanged"
    delivery: Annotated[type[HeartbeatDelivery], Argument(flags=("delivery",))]

    def apply(self, ctx: Comms) -> CliResult:
        updated = ctx.threads.set_heartbeat_delivery(self.name, self.delivery)
        return HeartbeatDeliveryConfiguredResult(updated.name, updated.heartbeat_delivery)
