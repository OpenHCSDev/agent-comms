"""Give pre-P479 host bridge bindings their own stream position once at install.

Run only after host bridges pause, before they resume; never in a bus rehearsal.
Delete this tool and its host call after the installation that runs it.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
from abc import ABC, abstractmethod
from contextlib import closing
from dataclasses import dataclass, fields
from pathlib import Path

from agent_comms.bus import Bus
from agent_comms.cli_bridges.shared import Binding, BindingIdentity, BridgeStateTable, DeliveryRow
from agent_comms.locked_store import SQLiteFile
from agent_comms.message_reference import BridgePosition, BridgeStart
from agent_comms.typed_table import TypedTable
from agent_comms_hooks.watch import WatchClaim, WatchRecord


@dataclass(frozen=True)
class PreviousBinding(BindingIdentity, TypedTable):
    """The three fields the bridge stored before it declared its start."""

    @classmethod
    def read_binding(cls, db: sqlite3.Connection) -> list[PreviousBinding]:
        columns = Binding._column_list(tuple(item.name for item in fields(BindingIdentity)))
        return cls.read(db.execute(f'SELECT {columns} FROM "{Binding.declared_name}"'))


class BindingMigration(ABC):
    @abstractmethod
    def report(self) -> int: ...


@dataclass(frozen=True)
class MigratedBindings(BindingMigration):
    changed: int

    def report(self) -> int:
        return self.changed


@dataclass(frozen=True)
class DeadBinding(BindingMigration):
    path: Path

    def report(self) -> int:
        print(f"dead binding, bus missing: {self.path}")
        return 0


def state_directories(home: Path) -> tuple[Path, ...]:
    """Recorded commands retain custom state paths; default roots retain inactive bindings."""
    states: set[Path] = set()
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--state", type=Path, action="append", default=[])
    parser.add_argument("--session", action="append", default=[])
    for path in WatchClaim.directory(home).glob("*.lock"):
        text = path.read_text()
        if not text:
            continue
        record = WatchRecord.decode(**json.loads(text))
        arguments, _ = parser.parse_known_args(record.command[1:])
        for root in arguments.state:
            state = root.joinpath(*arguments.session)
            states.update(path.parent for path in state.glob(BridgeStateTable.filename))
    for name in ("claude-comms-inbox", "codex-comms-inbox", "opencode-comms-inbox"):
        base = home / ".local/state" / name
        states.update(path.parent for path in base.rglob(BridgeStateTable.filename))
    return tuple(sorted(states))


def migrate(state: Path) -> BindingMigration:
    """Carry the binding table atomically, preserving every journal and observation row."""
    path = state / BridgeStateTable.filename
    if not path.is_file():
        raise FileNotFoundError(path)
    with closing(SQLiteFile.writer(path)) as db, SQLiteFile.write_transaction(db):
        (start,) = Binding._fields_named(("start",))
        if start.wire_name in Binding.stored_columns(db):
            return MigratedBindings(0)
        previous = PreviousBinding.read_binding(db)
        latest = DeliveryRow.latest(db)
        bindings = []
        for row in previous:
            position: BridgePosition
            if latest:
                position = BridgeStart(latest[0].message.reference)
            else:
                try:
                    position = Binding.bus_head(row.root)
                except FileNotFoundError:
                    return DeadBinding(Bus.path_in(Path(row.root)))
            bindings.append(Binding(row.root, row.bus, row.thread, position))
        Binding.drop(db)
        Binding.create(db)
        for binding in bindings:
            binding.insert(db)
    return MigratedBindings(len(bindings))


def revert(state: Path) -> BindingMigration:
    """Restore the pre-P479 binding declaration, leaving all journal rows intact."""
    path = state / BridgeStateTable.filename
    if not path.is_file():
        raise FileNotFoundError(path)
    with closing(SQLiteFile.writer(path)) as db, SQLiteFile.write_transaction(db):
        (start,) = Binding._fields_named(("start",))
        if start.wire_name not in Binding.stored_columns(db):
            if not DeliveryRow.latest(db):
                for binding in PreviousBinding.read_binding(db):
                    try:
                        Binding.bus_head(binding.root)
                    except FileNotFoundError:
                        return DeadBinding(Bus.path_in(Path(binding.root)))
            return MigratedBindings(0)
        previous = PreviousBinding.read_binding(db)
        Binding.drop(db)
        PreviousBinding.create(db)
        for binding in previous:
            binding.insert(db)
        db.execute(
            f'ALTER TABLE "{PreviousBinding.declared_name}" RENAME TO "{Binding.declared_name}"'
        )
    return MigratedBindings(len(previous))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--home", type=Path, required=True)
    parser.add_argument(
        "--revert", dest="operation", action="store_const", const=revert, default=migrate
    )
    arguments = parser.parse_args()
    changed = sum(
        arguments.operation(state).report() for state in state_directories(arguments.home)
    )
    print(f"Binding rows changed: {changed}")


if __name__ == "__main__":
    main()
