from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Annotated

from agent_comms.cli_support.store_reconcile import StoreReconcileCliCommand
from agent_comms.cli_verbs.cli_commands import ReconciledStoresResult, StoreDifference
from agent_comms.nominal.field_codec import Argument
from agent_comms.storage.typed_table import HeldBusFile, NoPractice, Practice, ReconciledFile


@dataclass(frozen=True, kw_only=True)
class ReconcileStoresCliCommand(
    StoreReconcileCliCommand[ReconciledStoresResult], declared_name=HeldBusFile.carry_verb,
):
    help = ("Reconcile every declared store's schema onto this runtime's declarations; "
            "the installer runs it under maintenance after the outgoing owners stop")

    practice: Annotated[Practice, Argument(flags=("--practice-root",))] = NoPractice()

    def answer(self, root: Path) -> ReconciledStoresResult:
        reconciled = tuple(
            (path.relative_to(root), differences)
            for path, owner in sorted(ReconciledFile.existing_in(root).items())
            if (differences := owner.reconcile_at(path, self.practice)))
        return ReconciledStoresResult(
            tuple(path for path, _ in reconciled),
            tuple(StoreDifference(path, difference)
                  for path, differences in reconciled for difference in differences))
