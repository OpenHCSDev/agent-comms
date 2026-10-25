"""Give every saved hunt an id in HuntId's spelling.

HuntRecord.id was retyped from text to HuntId (7b617b78b) while the column stayed TEXT, so
hunts recorded before then keep ids like ``P5:0`` that HuntId refuses, and every read of
the hunt table fails. Nothing else refers to a hunt by its id, so each refused id is
replaced by a fresh one. Delete this step after the live installation.
"""

from __future__ import annotations

from contextlib import closing
from typing import ClassVar

from agent_comms.field_codec import FieldCodec, HuntId, Id128Text
from agent_comms.installer.probes.carry import Carried, Carry
from agent_comms.locked_store import RootFile
from agent_comms.maintenance_barrier import MaintenanceBarrier, MaintenanceHolder
from agent_comms.registry_store import RegistryStore
from agent_comms.todos import HuntRecord, TodoStore


class HuntRecordIds(Carry):
    opened_files: ClassVar[tuple[type[RootFile], ...]] = (TodoStore,)

    def carry(self, *, apply: bool) -> Carried:
        path = TodoStore.path_in(self.root)
        self.require_confined(path)
        if not path.exists():
            return Carried(0)
        with TodoStore.observing(path) as db:
            if not HuntRecord.present(db):
                return Carried(0)
            refused = tuple(stored for (stored,) in db.execute(
                HuntRecord._sql(f'SELECT {{id}} FROM "{HuntRecord.declared_name}"'))
                if not Id128Text.spells(stored))
        if apply and refused:
            MaintenanceBarrier(RegistryStore.path_in(self.root)).read().require_held_by(
                MaintenanceHolder.current(), "Carry saved hunt ids"
            )
            (column,) = (field for field in HuntRecord._fields if field.column.primary_key)
            with closing(TodoStore.writer(path)) as db, db:
                db.execute("BEGIN IMMEDIATE")
                self.rewrite_stored(db, HuntRecord, column,
                                    ((FieldCodec.encode(HuntId.mint()), stored) for stored in refused))
        return Carried(
            len(refused),
            detail={"hunts": len(refused)},
            rewritten=(path.relative_to(self.root),) if apply and refused else (),
        )
