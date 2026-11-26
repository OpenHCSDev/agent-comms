from __future__ import annotations

from dataclasses import dataclass

from agent_comms.cli_verbs.cli_verb import CliBus, CliCommand
from agent_comms.registry.shared_ledger import LedgerDocument


@dataclass(frozen=True, kw_only=True)
class LedgerCliCommand(CliCommand):
    help = "Read ledger state"

    def apply(self, ctx: CliBus) -> LedgerDocument:
        return ctx.ledger.read()
