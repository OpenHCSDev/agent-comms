from __future__ import annotations

from agent_comms.cli_commands import ReconciledStoresResult
from agent_comms.cli_support.store_reconcile import StoreReconcileCliCommand
from agent_comms.nominal.field_codec import Argument
from agent_comms.typed_table import HeldBusFile
from agent_comms.typed_table import NoPractice
from agent_comms.typed_table import Practice
from agent_comms.typed_table import ReconciledFile
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated


@dataclass(frozen=True, kw_only=True)
class ReconcileStoresCliCommand(
    StoreReconcileCliCommand[ReconciledStoresResult], declared_name=HeldBusFile.carry_verb,
):
    help = ("Reconcile every declared store's schema onto this runtime's declarations; "
            "the installer runs it under maintenance after the outgoing owners stop")

    practice: Annotated[Practice, Argument(flags=("--practice-root",))] = NoPractice()

    def answer(self, root: Path) -> ReconciledStoresResult:
        return ReconciledStoresResult(tuple(
            path.relative_to(root)
            for path, owner in sorted(ReconciledFile.existing_in(root).items())
            if owner.reconcile_at(path, self.practice)))
