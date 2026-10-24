"""Give accepted PR heads recorded before verdicts carried a reason the reason they lack.

A carried record already holds its reason, so a second run changes nothing. Delete this
step after the live installation.
"""

from contextlib import closing
from typing import ClassVar

from agent_comms.field_codec import FieldCodec
from agent_comms.installer.probes.carry import Carried, Carry
from agent_comms.locked_store import RootFile
from agent_comms.maintenance_barrier import MaintenanceBarrier, MaintenanceHolder
from agent_comms.registry_store import RegistryStore
from agent_comms.todos import (
    DecidedHeadVerdict,
    ReviewedHeadVerdict,
    StoredHeadReview,
    TodoStore,
    VerdictReason,
)


class HeadVerdictReasons(Carry):
    opened_files: ClassVar[tuple[type[RootFile], ...]] = (TodoStore,)
    reason: ClassVar[VerdictReason] = VerdictReason(
        "This head was approved before head verdicts stored a reason, so none was kept."
    )

    def rewrite(self, record: type, payload: dict[str, object]) -> dict[str, object]:
        return {
            FieldCodec.record_key(DecidedHeadVerdict, "reason"): FieldCodec.encode(self.reason),
            **payload,
        }

    def carry(self, *, apply: bool) -> Carried:
        path = TodoStore.path_in(self.root)
        self.require_confined(self.root, path)
        if not path.exists():
            return Carried(0)
        (column,) = StoredHeadReview._fields_named(("record",))
        updates: list[tuple[object, int]] = []
        with TodoStore.observing(path) as db:
            if not StoredHeadReview.present(db):
                return Carried(0)
            for seq, stored in db.execute(
                StoredHeadReview._sql(
                    f'SELECT {{seq}}, {{record}} FROM "{StoredHeadReview.declared_name}"'
                )
            ):
                original = column.storage.from_sql(stored)
                current = column.value.rewrite_document(
                    original, self.rewrite, lambda record: record is ReviewedHeadVerdict
                )
                if current != original:
                    updates.append((column.storage.to_sql(current), seq))
        if apply and updates:
            MaintenanceBarrier(RegistryStore.path_in(self.root)).read().require_held_by(
                MaintenanceHolder.current(), "Carry head verdict reasons"
            )
            with closing(TodoStore.writer(path)) as db, TodoStore.write_transaction(db):
                self.rewrite_stored(db, StoredHeadReview, column, updates)
        return Carried(
            len(updates), rewritten=(path.relative_to(self.root),) if apply and updates else ()
        )
