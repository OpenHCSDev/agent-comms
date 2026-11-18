"""Human display evidence captured once for one presentation pass."""

from dataclasses import dataclass
from functools import cached_property
from pathlib import Path

from agent_comms.messaging.read_ledger import ReadDocument, ReadLedger


@dataclass(frozen=True)
class HumanReadCut:
    """One first read, retained only until this presentation pass is discarded."""

    root: Path

    @cached_property
    def ledger(self) -> ReadLedger:
        return ReadLedger.at(self.root)

    @cached_property
    def document(self) -> ReadDocument:
        return self.ledger.read()
