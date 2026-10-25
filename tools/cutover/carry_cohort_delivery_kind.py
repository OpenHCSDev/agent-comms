"""Retire the delivery discriminator only when it agrees with the retained claim."""

from contextlib import closing
from pathlib import Path
import sqlite3
from typing import ClassVar

from agent_comms.cohort_schema import ClaimBatchAcknowledgements, CohortDeliveries
from agent_comms.coordination_database import CoordinationStore
from agent_comms.errors import RelationViolationError
from agent_comms.installer.probes.carry import Carried, Carry
from agent_comms.locked_store import RootFile
from agent_comms.typed_table import SQLiteCatalogObject, SQLiteForeignKeys


class CohortDeliveryKind(Carry):
    opened_files: ClassVar[tuple[type[RootFile], ...]] = (CoordinationStore,)

    @staticmethod
    def require_consistent(db: sqlite3.Connection, path: Path) -> None:
        inconsistent = db.execute(
            "SELECT wire_root_id,wire_seq,ordinal FROM cohort_delivery_receipts "
            "WHERE (claim_id IS NOT NULL AND kind IS NOT 'selected') "
            "OR (claim_id IS NULL AND kind IS NOT 'unmentioned_observer') LIMIT 1"
        ).fetchone()
        if inconsistent is not None:
            raise RelationViolationError(
                f"{path}: delivery {tuple(inconsistent)} has a kind inconsistent with its claim"
            )

    def carry(self, *, apply: bool) -> Carried:
        pending: list[Path] = []
        for path in self.input_files():
            with closing(CoordinationStore.read_snapshot(path)) as db:
                if "kind" not in CohortDeliveries.stored_columns(db):
                    continue
                self.require_consistent(db, path)
            pending.append(path)
        if apply:
            for path in pending:
                with closing(CoordinationStore.writer(path)) as db:
                    with SQLiteForeignKeys.relations_suspended(db), CoordinationStore.write_transaction(db):
                        self.require_consistent(db, path)
                        SQLiteCatalogObject.release_guards(db, ClaimBatchAcknowledgements)
                        CohortDeliveries.rebuild(db)
                        ClaimBatchAcknowledgements.create(db, if_not_exists=True)
                        CohortDeliveries.require_schema(db)
        return Carried(
            len(pending),
            rewritten=tuple(path.relative_to(self.root) for path in pending) if apply else (),
        )
