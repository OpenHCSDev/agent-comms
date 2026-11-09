from __future__ import annotations

from abc import abstractmethod
from functools import partial

from agent_comms.cli_verbs.cli_command import CliCommand
from agent_comms.cli_verbs.cli_result import CliResult
from agent_comms.comms import Comms
from agent_comms.messaging.bus_write import HeldBusWrite


class PublishingCliCommand(HeldBusWrite, CliCommand, declared_name=None):
    """Wait for maintenance to clear before running a CLI publication."""

    def apply(self, ctx: Comms) -> CliResult:
        return self.write_when_open(ctx, partial(self.publish, ctx))

    @abstractmethod
    def publish(self, ctx: Comms) -> CliResult: ...
