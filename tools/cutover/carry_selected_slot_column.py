"""Rename the selected tool slot's stored input_id column to its declared slot_id."""

from contextlib import closing
from typing import ClassVar

from agent_comms.coordination_database import CoordinationStore
from agent_comms.errors import RelationViolationError
from agent_comms.installer.probes.carry import Carried, Carry
from agent_comms.locked_store import RootFile
from agent_comms.native_runtime_input import SelectedToolSlot


class SelectedSlotColumn(Carry):
    opened_files: ClassVar[tuple[type[RootFile], ...]] = (CoordinationStore,)
    previous: ClassVar[str] = "input_id"
    declared: ClassVar[str] = "slot_id"

    def carry(self, *, apply: bool) -> Carried:
        renamed = []
        for path in self.input_files():
            with closing(CoordinationStore.read_snapshot(path)) as db:
                if not SelectedToolSlot.present(db):
                    continue
                stored = set(SelectedToolSlot.stored_columns(db))
            if self.previous not in stored:
                continue
            if self.declared in stored:
                raise RelationViolationError(
                    f"{path}: {self.previous} and {self.declared} could each hold the slot;"
                    " refusing to choose")
            renamed.append(path)
        if apply:
            for path in renamed:
                with (closing(CoordinationStore.writer(path)) as db,
                      CoordinationStore.write_transaction(db)):
                    SelectedToolSlot.rename_stored_column(db, self.previous, self.declared)
        return Carried(len(renamed), rewritten=tuple(
            path.relative_to(self.root) for path in renamed) if apply else ())
