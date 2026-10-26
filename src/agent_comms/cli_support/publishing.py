from __future__ import annotations

from abc import abstractmethod
from agent_comms.bus_write import HeldBusWrite
from agent_comms.cli_command import CliCommand
from agent_comms.cli_result import CliResult
from agent_comms.comms import Comms
from functools import partial


class PublishingCliCommand(HeldBusWrite, CliCommand, declared_name=None):
    """Wait for maintenance to clear before running a CLI publication."""

    def apply(self, ctx: Comms) -> CliResult:
        return self.write_when_open(ctx, partial(self.publish, ctx))

    @abstractmethod
    def publish(self, ctx: Comms) -> CliResult: ...
