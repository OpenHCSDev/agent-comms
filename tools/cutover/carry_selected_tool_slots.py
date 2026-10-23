"""Move the selected-tool files into their one coordination row, then retire them.

The filename and its contents are the original slot identity. No input table or
transcript can reconstruct them. Delete this carry after every root has crossed.
"""

from __future__ import annotations

from contextlib import closing
from dataclasses import dataclass
from pathlib import Path

from agent_comms.child_process import Platform
from agent_comms.coordination_database import CoordinationStore
from agent_comms.errors import RelationViolationError
from agent_comms.installer.carry import Carried, Carry
from agent_comms.locked_store import SQLiteFile
from agent_comms.native_runtime_input import (
    AcknowledgedSlot,
    ConsumedSlot,
    SelectedSlotState,
    SelectedToolSlot,
)
from agent_comms.native_session_files import SavedSessionFile
from agent_comms.typed_table import HeldBusFile


@dataclass(frozen=True)
class LedgerSlot:
    """One legacy consumed file and its optional matching acknowledgement."""

    consumed: Path
    acknowledgements: tuple[Path, ...]

    @property
    def files(self) -> tuple[Path, ...]:
        return (self.consumed, *self.acknowledgements)

    def read(self) -> SelectedToolSlot:
        original = self.consumed.read_bytes()
        state: type[SelectedSlotState] = ConsumedSlot
        for acknowledgement in self.acknowledgements:
            if acknowledgement.read_bytes() != original:
                raise RelationViolationError(
                    f"Selected tool acknowledgement differs from {self.consumed}: {acknowledgement}"
                )
            state = AcknowledgedSlot
        return SelectedToolSlot(
            self.consumed.name, original.decode("ascii").removesuffix("\n"), state,
        )


class SelectedToolSlots(Carry):
    opened_files = (CoordinationStore,)

    def slots(self) -> tuple[LedgerSlot, ...]:
        slots = []
        directories = {
            path for owner, root in SavedSessionFile.declarations_in(self.root)
            for path in root.glob(str(owner.pattern().parent / "selected-tool-ledger"))
        }
        for directory in sorted(directories):
            self.require_confined(self.root, directory)
            files = frozenset(directory.iterdir())
            for path in files:
                self.require_confined(self.root, path)
                if not path.is_file():
                    raise RelationViolationError(f"Selected tool ledger entry is not a file: {path}")
                if path.suffix == ".done" and path.with_suffix("") not in files:
                    raise RelationViolationError(f"Selected tool acknowledgement has no consumed file: {path}")
            slots.extend(
                LedgerSlot(path, tuple(ack for ack in (path.with_name(path.name + ".done"),)
                                       if ack in files))
                for path in sorted(files) if path.suffix != ".done"
            )
        return tuple(slots)

    def input_files(self) -> tuple[Path, ...]:
        return (*super().input_files(), *(path for slot in self.slots() for path in slot.files))

    def carry(self, *, apply: bool) -> Carried:
        slots = self.slots()
        if not slots:
            return Carried(0)
        rows = tuple(slot.read() for slot in slots)
        files = tuple(path for slot in slots for path in slot.files)
        acknowledged = sum(len(slot.acknowledgements) for slot in slots)
        detail = {
            "consumed_files": len(slots), "acknowledgement_files": acknowledged,
            "consumed_rows": len(slots) - acknowledged, "acknowledged_rows": acknowledged,
            "slot_rows": len(slots), "deleted_files": len(files) if apply else 0,
        }
        if not apply:
            return Carried(len(files), detail)
        path = CoordinationStore.path_in(self.root)
        self.require_confined(self.root, path)
        HeldBusFile(CoordinationStore, path).require()
        with closing(SQLiteFile.writer(path)) as db, SQLiteFile.write_transaction(db):
            SelectedToolSlot.create(db, if_not_exists=True)
            for row in rows:
                row.carry(db)
        # Every row is committed before the first unlink. Persist removal of
        # acknowledgements before removing consumed files: a killed carry can
        # leave a consumed file, but can never manufacture an orphan .done.
        directories = tuple(dict.fromkeys(slot.consumed.parent for slot in slots))
        for slot in slots:
            for acknowledgement in slot.acknowledgements:
                acknowledgement.unlink()
        for directory in directories:
            Platform.current().sync_directory(directory)
        for slot in slots:
            slot.consumed.unlink()
        for directory in directories:
            directory.rmdir()
            Platform.current().sync_directory(directory.parent)
        return Carried(len(files), detail,
                       (path.relative_to(self.root), *(file.relative_to(self.root) for file in files)))
