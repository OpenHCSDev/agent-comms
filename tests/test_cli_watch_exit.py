"""Writer contention keeps the watch child; terminal failures end it."""
import asyncio
from contextlib import suppress
from dataclasses import replace
import os
import signal
import sys

import pytest

from agent_comms.child_process import AttachedChild
from agent_comms.cli_bridges.shared import Binding, CliBridge, Operation, Options
from agent_comms.cli_protocol import DeliveredOutcome, FailedCoreError


class WatchOnly(Operation):
    async def execute(self, bridge):
        await bridge.watch()


class ReportingBridge(CliBridge):
    async def deliver(self, message):
        raise AssertionError('No message may be delivered in an exit check')

    async def show_sent(self, result):
        raise AssertionError('No send in an exit check')

    async def report_failure(self, text):
        self.failures.append(text)


PROBE = ("from cutover.carry import Carried, Carry\n\n\n"
         "class Probe(Carry):\n"
         "    @classmethod\n    def carry(cls, root, *, apply):\n"
         "        (root / 'carried').write_text('carried')\n        return Carried(1, {}, ('carried',))\n\n\n"
         "if __name__ == '__main__':\n    Probe.main()\n")


