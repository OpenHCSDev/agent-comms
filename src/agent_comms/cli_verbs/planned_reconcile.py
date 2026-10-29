from __future__ import annotations

from agent_comms.cli_commands import PlannedStoresResult
from agent_comms.cli_support.store_reconcile import StoreReconcileCliCommand
from agent_comms.field_codec import Argument
from agent_comms.storage_rules import StorageRule
from agent_comms.typed_table import ReconciledFile
from agent_comms.typed_table import StoreRewrite
from agent_comms.typed_table import ExternalStore
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated


@dataclass(frozen=True, kw_only=True)
class PlannedReconcileCliCommand(
    StoreReconcileCliCommand[PlannedStoresResult], declared_name="planned-reconcile",
):
    carrying: Annotated[tuple[type[ExternalStore], ...], Argument(options={"nargs": "*"})] = ()
    """These declarations are validated after their pending carries, not before them."""
    help = ("Name every store reconcile-stores would rewrite, reading each without writing; "
            "the installer backs them up and checks them against its rehearsal first")

    def answer(self, root: Path) -> PlannedStoresResult:
        """A store outside the bus that the target's writer could not open refuses the plan."""
        ExternalStore.practice_openings(
            root, scratch=StorageRule.load(root).scratch_base, excluding=self.carrying)
        rewrites: list[StoreRewrite] = []
        for path, owner in sorted(ReconciledFile.existing_in(root).items()):
            schema = owner.schema_at(path)
            with closing(owner.read_snapshot(path)) as db:
                rewrites.extend(schema.reconcile_plan(db, path.relative_to(root)))
        return PlannedStoresResult(tuple(rewrites))
