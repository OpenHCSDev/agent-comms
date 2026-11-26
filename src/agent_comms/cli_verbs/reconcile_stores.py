from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Annotated

from agent_comms.cli_support.store_reconcile import StoreReconcileCliCommand
from agent_comms.cli_verbs.cli_result import CliResult
from agent_comms.nominal.field_codec import Argument, PathText
from agent_comms.storage.typed_table import (
    HeldBusFile,
    NoPractice,
    Practice,
    ReconciledFile,
    SQLiteSchemaDifference,
)


@dataclass(frozen=True)
class StoreDifference:
    """One schema object a reconcile rebuilt a store for."""

    path: Annotated[Path, PathText]
    difference: SQLiteSchemaDifference


@dataclass(frozen=True)
class ReconciledStoresResult(CliResult):
    """The bus files whose schema the reconcile changed, relative to the bus root, and the
    objects that made each one differ. An outgoing runtime's reply names no differences: the
    installer's restore (installer/upgrade.py restore_stores) decodes it with this class."""

    rewritten: tuple[Annotated[Path, PathText], ...]
    differences: tuple[StoreDifference, ...] = ()


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
