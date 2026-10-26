"""Declaration-owned CLI operation and its required dependencies."""

from __future__ import annotations

from agent_comms.cli_command import CliCommand
from agent_comms.comms import Comms
from agent_comms.shared_ledger import LedgerDocument
from dataclasses import dataclass


@dataclass(frozen=True, kw_only=True)
class LedgerCliCommand(CliCommand):
    help = "Read ledger state"

    def apply(self, ctx: Comms) -> LedgerDocument:
        return ctx.ledger.read()
