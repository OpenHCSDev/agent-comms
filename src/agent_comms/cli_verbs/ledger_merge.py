from __future__ import annotations

from agent_comms.cli_command import CliCommand
from agent_comms.cli_commands import LedgerMergedResult
from agent_comms.comms import Comms
from agent_comms.field_codec import Argument
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated
import json


@dataclass(frozen=True, kw_only=True)
class LedgerMergeCliCommand(CliCommand, declared_name="ledger-merge"):
    help = "Merge ledger updates from a JSON file"
    merge_from: Annotated[str, Argument(flags=("--merge-from",))]
    author: Annotated[str, Argument(flags=("--author",))]

    def apply(self, ctx: Comms) -> LedgerMergedResult:
        ctx.ledger.merge(json.loads(Path(self.merge_from).read_text()), self.author)
        return LedgerMergedResult()
