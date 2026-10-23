"""Move the selected-tool files into their one coordination row, then retire them.

The filename and its contents are the original slot identity. No input table or
transcript can reconstruct them. Delete this carry after every root has crossed.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar

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
class LedgerSlot(ABC):
    """One legacy slot; its file kind owns validation and the declared state."""

    consumed: Path

    @property
    @abstractmethod
    def state(self) -> type[SelectedSlotState]: ...

    @classmethod
    def at(cls, consumed: Path, acknowledgements: frozenset[Path]) -> LedgerSlot:
        acknowledgement = consumed.with_name(consumed.name + ".done")
        if acknowledgement in acknowledgements:
            return AcknowledgedLedgerSlot(consumed, acknowledgement)
        return ConsumedLedgerSlot(consumed)

    @property
    def files(self) -> tuple[Path, ...]:
        return (self.consumed,)

    def require_acknowledgement(self, original: bytes) -> None:
        """A consumed file does not claim a terminal acknowledgement."""

    def retire_acknowledgement(self) -> None:
        """A consumed file has no acknowledgement file to retire."""

    def read(self) -> SelectedToolSlot:
        original = self.consumed.read_bytes()
        self.require_acknowledgement(original)
        return SelectedToolSlot(
            self.consumed.name,
            original.decode("ascii").removesuffix("\n"),
            self.state,
        )


class ConsumedLedgerSlot(LedgerSlot):
    state: ClassVar[type[SelectedSlotState]] = ConsumedSlot


@dataclass(frozen=True)
class AcknowledgedLedgerSlot(LedgerSlot):
    acknowledgement: Path
    state: ClassVar[type[SelectedSlotState]] = AcknowledgedSlot

    @property
    def files(self) -> tuple[Path, ...]:
        return (self.consumed, self.acknowledgement)

    def require_acknowledgement(self, original: bytes) -> None:
        if self.acknowledgement.read_bytes() != original:
            raise RelationViolationError(
                f"Selected tool acknowledgement differs from {self.consumed}: {self.acknowledgement}"
            )

    def retire_acknowledgement(self) -> None:
        self.acknowledgement.unlink()


class SelectedToolSlots(Carry):
    opened_files = (CoordinationStore,)

    def directories(self) -> tuple[Path, ...]:
        directories = {
            path
            for owner, root in SavedSessionFile.declarations_in(self.root)
            for path in root.glob(str(owner.pattern().parent / "selected-tool-ledger"))
        }
        for directory in directories:
            self.require_confined(self.root, directory)
        return tuple(sorted(directories))

    def slots(self) -> tuple[LedgerSlot, ...]:
        slots = []
        for directory in self.directories():
            files = frozenset(directory.iterdir())
            for path in files:
                self.require_confined(self.root, path)
                if not path.is_file():
                    raise RelationViolationError(
                        f"Selected tool ledger entry is not a file: {path}"
                    )
            acknowledgements = frozenset(path for path in files if path.match("*.done"))
            consumed = files - acknowledgements
            for path in acknowledgements:
                if path.with_suffix("") not in consumed:
                    raise RelationViolationError(
                        f"Selected tool acknowledgement has no consumed file: {path}"
                    )
            slots.extend(LedgerSlot.at(path, acknowledgements) for path in sorted(consumed))
        return tuple(slots)

    def input_files(self) -> tuple[Path, ...]:
        return (*super().input_files(), *(path for slot in self.slots() for path in slot.files))

    def carry(self, *, apply: bool) -> Carried:
        slots = self.slots()
        directories = self.directories()
        if not directories:
            return Carried(0)
        rows = tuple(slot.read() for slot in slots)
        files = tuple(path for slot in slots for path in slot.files)
        acknowledged = len(files) - len(slots)
        detail: dict[str, int | bool] = {
            "consumed_files": len(slots),
            "acknowledgement_files": acknowledged,
            "consumed_rows": len(slots) - acknowledged,
            "acknowledged_rows": acknowledged,
            "slot_rows": len(slots),
            "deleted_files": len(files) if apply else 0,
        }
        changed = len(files) + len(directories) - len({slot.consumed.parent for slot in slots})
        if not apply:
            return Carried(changed, detail)
        path = CoordinationStore.path_in(self.root)
        self.require_confined(self.root, path)
        HeldBusFile(CoordinationStore, path).require()
        if rows:
            with closing(SQLiteFile.writer(path)) as db, SQLiteFile.write_transaction(db):
                SelectedToolSlot.create(db, if_not_exists=True)
                for slot, row in zip(slots, rows, strict=True):
                    if row.insert_if_absent(db).rowcount == 0:
                        current = SelectedToolSlot.require(db, input_id=row.input_id)
                        if current.call_id != row.call_id or current.state is not row.state:
                            raise RelationViolationError(
                                f"Selected tool slot differs from committed row: {slot.consumed}"
                            )
                        current.require_call(row.call_id)
        # Every row is committed before the first unlink. Persist removal of
        # acknowledgements before removing consumed files: a killed carry can
        # leave a consumed file, but can never manufacture an orphan .done.
        for slot in slots:
            slot.retire_acknowledgement()
        for directory in directories:
            Platform.current().sync_directory(directory)
        for slot in slots:
            slot.consumed.unlink()
        for directory in directories:
            directory.rmdir()
            Platform.current().sync_directory(directory.parent)
        return Carried(
            changed,
            detail,
            (path.relative_to(self.root), *(file.relative_to(self.root) for file in files)),
        )
