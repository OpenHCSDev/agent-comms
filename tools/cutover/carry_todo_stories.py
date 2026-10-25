"""Move inline Todo histories to their append-only table once.

Delete after every retained installation has crossed this carry. Current readers
never accept the old row shape; a failed carry rolls its entire transaction back.
"""
from __future__ import annotations

from contextlib import closing
from typing import ClassVar

from agent_comms.field_codec import FieldCodec
from agent_comms.installer.probes.carry import Carried, Carry
from agent_comms.locked_store import RootFile
from agent_comms.maintenance_barrier import MaintenanceBarrier, MaintenanceHolder
from agent_comms.registry_store import RegistryStore
from agent_comms.recorded_time import UnrecordedEventTime
from agent_comms.todos import CheckedProblemOrder, OrderMessage, StoredOrderStory, Todo, TodoCommand, TodoStore

# This is the retired external schema, consumed only by this one-shot boundary.
INLINE_FIELDS = ("strikes", "voided", "released", "verdicts", "corrections", "messages", "prior_orders")


class TodoStories(Carry):
    opened_files: ClassVar[tuple[type[RootFile], ...]] = (TodoStore,)

    def carry(self, *, apply: bool) -> Carried:
        path = TodoStore.path_in(self.root)
        self.require_confined(path)
        if not path.exists():
            return Carried(0)
        with TodoStore.observing(path) as db:
            present = set(Todo.stored_columns(db)).intersection(INLINE_FIELDS)
            if not present:
                return Carried(0)
            if present != set(INLINE_FIELDS):
                raise ValueError(f"{path}: incomplete inline Todo history: {sorted(present)}")
            count, = db.execute(f'SELECT count(*) FROM "{Todo.declared_name}"').fetchone()
        if not apply:
            return Carried(1, detail={"todos": count})
        MaintenanceBarrier(RegistryStore.path_in(self.root)).read().require_held_by(
            MaintenanceHolder.current(), "Carry Todo stories",
        )
        events = 0
        with closing(TodoStore.writer(path)) as db, TodoStore.write_transaction(db):
            StoredOrderStory.create(db)
            columns = ",".join(f'"{name}"' for name in INLINE_FIELDS)
            for problem, *histories in db.execute(
                f'SELECT id,{columns} FROM "{Todo.declared_name}" ORDER BY rowid'
            ).fetchall():
                decoded: list[TodoCommand | CheckedProblemOrder] = []
                for name, history in zip(INLINE_FIELDS, histories, strict=True):
                    raw = FieldCodec.parse(history)
                    if not isinstance(raw, list):
                        raise ValueError(f"{problem}.{name}: expected the retired event array")
                    for entry in raw:
                        if not isinstance(entry, dict):
                            raise ValueError(f"{problem}.{name}: expected a retired event record")
                        entry.setdefault(FieldCodec.record_key(OrderMessage, "at"),
                                         FieldCodec.encode(UnrecordedEventTime()))
                    decoded.extend(FieldCodec.decode(tuple[TodoCommand | CheckedProblemOrder, ...], raw))
                # The old strike tuple precedes its voids, including undated ones.
                # Story rendering retains each event's recorded time, not carry time.
                for event in decoded:
                    StoredOrderStory.append(db, problem, event)
                    events += 1
            Todo.rebuild(db)
        return Carried(1, detail={"todos": count, "events": events},
                       rewritten=(path.relative_to(self.root),))
