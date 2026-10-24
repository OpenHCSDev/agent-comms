"""Give pre-P479 host bridge bindings their own stream position once at install.

Run only after host bridges pause, before they resume; never in a bus rehearsal.
Delete this tool and its host call after the installation that runs it.
"""

from __future__ import annotations

import argparse
import fcntl
import json
import shutil
import sqlite3
from abc import ABC, abstractmethod
from contextlib import closing
from dataclasses import dataclass, fields, replace
from pathlib import Path
from typing import Annotated

from agent_comms.bus import Bus
from agent_comms.cli_bridges.shared import (
    Binding,
    BindingIdentity,
    BridgeStateTable,
    DeliveryRow,
    JournalMessage,
)
from agent_comms.cli_protocol import FilteredOutcome, Outcome
from agent_comms.field_codec import FieldCodec, PathText
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


@dataclass(frozen=True)
class PreviousWatch:
    """The saved command before its base directory became a declared field."""

    bus: str
    root: str
    command: tuple[str, ...]


@dataclass(frozen=True)
class WatchDirectory:
    path: Path
    record: WatchRecord
    state: Path
    session: str

    def held(self) -> bool:
        with self.path.open() as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_SH | fcntl.LOCK_NB)
            except BlockingIOError:
                return True
        return False

    def moved(self, state: Path) -> WatchRecord:
        parser = argparse.ArgumentParser(add_help=False)
        parser.add_argument('--state')
        parser.add_argument('--session')
        parser.add_argument('--bus-thread')
        _, remaining = parser.parse_known_args(self.record.command[2:])
        return replace(self.record, command=(
            *self.record.command[:2], *remaining, '--state', str(state),
        ), base_state=state.parent)


def recorded_watches(home: Path) -> tuple[WatchDirectory, ...]:
    """Decode old commands only here; new runtime readers require the typed base_state."""
    watches = []
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument('--state', type=Path, action='append', default=[])
    parser.add_argument('--session', action='append', default=[])
    parser.add_argument('--bus-thread', action='append', default=[])
    for path in sorted(WatchClaim.directory(home).glob('*.lock')):
        text = path.read_text()
        if not text:
            continue
        payload = json.loads(text)
        previous = FieldCodec.decode(PreviousWatch, {
            key: payload[key] for _, key in FieldCodec.record_fields(PreviousWatch)
        })
        arguments, _ = parser.parse_known_args(previous.command[2:])
        for base in arguments.state:
            candidates = (base.joinpath(*arguments.session),
                          base.joinpath(*arguments.bus_thread), base)
            existing = tuple(state for state in dict.fromkeys(candidates)
                             if (state / BridgeStateTable.filename).is_file())
            if not existing:
                continue
            state = existing[0]
            with closing(sqlite3.connect(
                    (state / BridgeStateTable.filename).as_uri() + '?mode=ro', uri=True)) as db:
                identity, = PreviousBinding.read_binding(db)
            base_state = base
            if state.name in (identity.thread, identity.bus):
                base_state = state.parent
            if 'base_state' in payload:
                base_state = Path(payload['base_state'])
            record = WatchRecord(previous.bus, previous.root, previous.command, base_state)
            if (identity.root, identity.bus) != (record.root, record.bus):
                raise ValueError(f'Watch and binding disagree: {path}, {state}')
            watches.append(WatchDirectory(path, record, state, identity.thread))
    return tuple(watches)


def state_directories(home: Path) -> tuple[Path, ...]:
    """Recorded commands retain custom state paths; default roots retain inactive bindings."""
    states = {watch.state for watch in recorded_watches(home)}
    for name in ("claude-comms-inbox", "codex-comms-inbox", "opencode-comms-inbox"):
        base = home / ".local/state" / name
        states.update(path.parent for path in base.rglob(BridgeStateTable.filename)
                      if '.binding-start-backup' not in path.parts)
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


@dataclass(frozen=True)
class SavedJournal:
    state: Annotated[Path, PathText]
    saved: Annotated[Path, PathText]

    def save(self) -> None:
        self.saved.mkdir(parents=True)
        for path in self.state.glob(BridgeStateTable.filename + '*'):
            shutil.copy2(path, self.saved / path.name)

    def restore(self) -> None:
        self.state.mkdir(parents=True, exist_ok=True)
        for path in self.state.glob(BridgeStateTable.filename + '*'):
            path.unlink()
        for path in self.saved.iterdir():
            shutil.copy2(path, self.state / path.name)


@dataclass(frozen=True)
class SavedWatch:
    path: Annotated[Path, PathText]
    saved: Annotated[Path, PathText]

    def save(self) -> None:
        self.saved.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(self.path, self.saved)

    def restore(self) -> None:
        # Preserve the lock inode held across the install's process replacement.
        with self.path.open('r+') as lock:
            lock.write(self.saved.read_text())
            lock.truncate()
            lock.flush()


@dataclass(frozen=True)
class DirectoryBackup:
    journals: tuple[SavedJournal, ...]
    watches: tuple[SavedWatch, ...]
    destinations: tuple[Annotated[Path, PathText], ...]

    def save(self, path: Path) -> None:
        for journal in self.journals:
            journal.save()
        for watch in self.watches:
            watch.save()
        path.write_text(FieldCodec.compact(FieldCodec.encode(self)))

    def restore(self) -> int:
        for destination in self.destinations:
            for path in destination.glob(BridgeStateTable.filename + '*'):
                path.unlink()
        for journal in self.journals:
            journal.restore()
        for watch in self.watches:
            watch.restore()
        for destination in self.destinations:
            if destination.is_dir() and not tuple(destination.iterdir()):
                destination.rmdir()
        return len(self.journals)


@dataclass(frozen=True)
class JournalDelivery(JournalMessage):
    outcome: type[Outcome]

    def preferred_to(self, previous: JournalDelivery) -> bool:
        """A confirmed delivery cannot become an uncertain attempt during consolidation."""
        if self.payload != previous.payload:
            raise ValueError(f'Different payloads for delivery {self.id}')
        return issubclass(self.outcome, FilteredOutcome) and not issubclass(
            previous.outcome, FilteredOutcome)


@dataclass(frozen=True)
class ReaderDirectories:
    identity: BindingIdentity
    states: tuple[Path, ...]
    watches: tuple[WatchDirectory, ...]

    def selected_watch(self) -> WatchDirectory:
        live = tuple(watch for watch in self.watches if watch.held())
        candidates = live or self.watches
        sessions = {watch.session for watch in candidates}
        if len(sessions) != 1:
            raise ValueError(f'Expected one transport for {self.identity.bus}: {sessions}')
        return candidates[0]

    @property
    def destination(self) -> Path:
        return self.selected_watch().record.base_state / self.identity.bus

    def merge(self) -> None:
        bindings = []
        for state in self.states:
            migrate(state).report()
            with Binding.open(state) as db:
                bindings.extend(Binding.select(db))
        position = max((binding.start for binding in bindings), key=lambda start: start.cursor())
        if self.identity.bus == 'claude-code':
            head = Binding.bus_head(self.identity.root)
            print(f'claude-code: start {position.cursor()} -> {head.cursor()}; '
                  f'skipped {head.cursor() - position.cursor()} bus positions')
            position = head
        selected = self.selected_watch()
        destination = self.destination
        merged = destination.parent / '.binding-start-backup' / (self.identity.bus + '.sqlite3')
        with Binding.open(merged.parent / (self.identity.bus + '.merging')) as target:
            with SQLiteFile.write_transaction(target):
                deliveries: dict[str, JournalDelivery] = {}
                for state in self.states:
                    with closing(sqlite3.connect(
                            (state / BridgeStateTable.filename).as_uri() + '?mode=ro',
                            uri=True)) as source:
                        for table in TypedTable.members_with(BridgeStateTable):
                            if table is Binding:
                                continue
                            columns = table._column_list(tuple(table.column_projection()))
                            for values in source.execute(
                                    f'SELECT {columns} FROM "{table.declared_name}"'):
                                if table is DeliveryRow:
                                    incoming, = JournalDelivery.read(source.execute(
                                        f'SELECT {JournalDelivery._select_list(DeliveryRow)} '
                                        f'FROM "{DeliveryRow.declared_name}" WHERE id=?',
                                        (values[0],)))
                                    if incoming.id in deliveries:
                                        if not incoming.preferred_to(deliveries[incoming.id]):
                                            continue
                                        DeliveryRow.delete(target, id=incoming.id)
                                    deliveries[incoming.id] = incoming
                                target.execute(table.insertion_statement() +
                                               ' ON CONFLICT DO NOTHING', values)
                Binding(self.identity.root, self.identity.bus,
                        selected.session, position).insert(target)
            destination.mkdir(parents=True, exist_ok=True)
            with closing(SQLiteFile.writer(merged)) as output:
                target.backup(output)
        for state in self.states:
            for path in state.glob(BridgeStateTable.filename + '*'):
                path.unlink()
        shutil.copy2(merged, destination / BridgeStateTable.filename)
        for watch in self.watches:
            with watch.path.open('r+') as lock:
                lock.write(watch.moved(destination).to_json())
                lock.truncate()
                lock.flush()
        print(f'{self.identity.bus}: merged {len(self.states)} stores, '
              f'{len(deliveries)} deliveries -> {destination}; session {selected.session}; '
              f'start {position.cursor()}')


def merge_directories(home: Path) -> int:
    watches = recorded_watches(home)
    groups: dict[tuple[str, str], list[Path]] = {}
    identities = {}
    for state in state_directories(home):
        with closing(sqlite3.connect(
                (state / BridgeStateTable.filename).as_uri() + '?mode=ro', uri=True)) as db:
            identity, = PreviousBinding.read_binding(db)
        key = (identity.root, identity.bus)
        identities[key] = identity
        groups.setdefault(key, []).append(state)
    readers = tuple(ReaderDirectories(identities[key], tuple(states), tuple(
        watch for watch in watches if (watch.record.root, watch.record.bus) == key
    )) for key, states in groups.items())
    by_base: dict[Path, list[ReaderDirectories]] = {}
    for reader in readers:
        if not reader.watches:
            # An inactive default binding still owns its declared session.
            raise ValueError(f'No saved watch for {reader.identity.bus}: {reader.states}')
        try:
            Binding.bus_head(reader.identity.root)
        except FileNotFoundError:
            DeadBinding(Bus.path_in(Path(reader.identity.root))).report()
            continue
        by_base.setdefault(reader.destination.parent, []).append(reader)
    changed = 0
    for base, members in by_base.items():
        directory = base / '.binding-start-backup'
        manifest = directory / 'manifest.json'
        if manifest.is_file():
            print(f'Already merged: {base}')
            continue
        sources = tuple(dict.fromkeys(state for member in members for state in member.states))
        commands = tuple(dict.fromkeys(
            watch.path for member in members for watch in member.watches))
        backup = DirectoryBackup(
            tuple(SavedJournal(state, directory / 'journals' / str(index))
                  for index, state in enumerate(sources)),
            tuple(SavedWatch(path, directory / 'watches' / path.name) for path in commands),
            tuple(member.destination for member in members),
        )
        backup.save(manifest)
        for member in members:
            member.merge()
            changed += len(member.states)
    return changed


def revert(home: Path) -> int:
    bases = {watch.record.base_state for watch in recorded_watches(home)}
    bases.update(home / '.local/state' / name for name in (
        'claude-comms-inbox', 'codex-comms-inbox', 'opencode-comms-inbox'))
    changed = 0
    for base in sorted(bases):
        directory = base / '.binding-start-backup'
        manifest = directory / 'manifest.json'
        if manifest.is_file():
            backup = FieldCodec.decode(DirectoryBackup, json.loads(manifest.read_text()))
            changed += backup.restore()
            shutil.rmtree(directory)
    return changed


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--home", type=Path, required=True)
    parser.add_argument(
        "--revert", dest="operation", action="store_const", const=revert,
        default=merge_directories
    )
    arguments = parser.parse_args()
    changed = arguments.operation(arguments.home)
    print(f"Binding rows changed: {changed}")


if __name__ == "__main__":
    main()
