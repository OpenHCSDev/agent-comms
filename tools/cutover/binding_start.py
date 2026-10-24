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
from dataclasses import dataclass, replace
from importlib.metadata import entry_points
from pathlib import Path
from typing import Annotated

from agent_comms.cli_bridges.claude import ClaudeBridge
from agent_comms.cli_bridges.codex import CodexBridge
from agent_comms.cli_bridges.opencode import OpenCodeBridge
from agent_comms.cli_bridges.shared import (
    Binding,
    BindingIdentity,
    BridgeStateTable,
    DeliveryRow,
    JournalMessage,
    ObservedPart,
)
from agent_comms.cli_protocol import FilteredOutcome, Outcome
from agent_comms.field_codec import FieldCodec, PathText, WireName
from agent_comms.locked_store import SQLiteFile
from agent_comms.message_reference import BridgePosition, BridgeStart
from agent_comms.registry_document import RegistrySnapshot
from agent_comms.registry_store import RegistryStore
from agent_comms.thread_identity import ThreadIncarnation
from agent_comms.typed_table import TypedRow
from agent_comms_hooks.watch import WatchClaim, WatchRecord


@dataclass(frozen=True)
class PreviousBinding(TypedRow):
    """The three fields the bridge stored before it declared its start."""

    root: str
    bus: Annotated[str, WireName("bus_thread")]
    thread: Annotated[str, WireName("session")]

    @classmethod
    def read_binding(cls, db: sqlite3.Connection) -> list[PreviousBinding]:
        owner, = Binding._fields_named(("owner",))
        if owner.wire_name in Binding.stored_columns(db):
            return [cls(row.root, row.bus, row.thread) for row in Binding.select(db)]
        columns = ', '.join(f'"{item}"' for item in cls.columns())
        return cls.read(db.execute(f'SELECT {columns} FROM "{Binding.declared_name}"'))

    def registered(self, db: sqlite3.Connection) -> Binding:
        owner, = Binding._fields_named(("owner",))
        if owner.wire_name in Binding.stored_columns(db):
            binding, = Binding.select(db)
            return binding
        with RegistryStore(RegistryStore.path_in(Path(self.root))).reading_snapshot() as registry:
            current = registry.threads.get(registry.canonical_name(self.bus))
        first, = db.execute(
            f'SELECT MIN(json_extract(payload, \'$.timestamp\')) FROM "{DeliveryRow.declared_name}"'
        ).fetchone()
        if current is None or first is None or current.incarnation.wire_birth > first:
            raise ValueError(
                f"Refused binding {self.root}/{self.bus}/{self.thread}: "
                f"no current birth proven before first delivery {first}; "
                "journal retained for explicit rebinding"
            )
        start, = Binding._fields_named(("start",))
        if start.wire_name in Binding.stored_columns(db):
            raw, = db.execute(
                f'SELECT "{start.wire_name}" FROM "{Binding.declared_name}"'
            ).fetchone()
            position = FieldCodec.decode(BridgePosition, json.loads(raw))
        else:
            latest = DeliveryRow.latest(db)
            position = (BridgeStart(latest[0].message.reference) if latest
                        else Binding.bus_head(self.root))
        return Binding(root=self.root, owner=current.incarnation,
                       thread=self.thread, start=position)


class BindingMigration(ABC):
    @abstractmethod
    def report(self) -> int: ...


@dataclass(frozen=True)
class MigratedBindings(BindingMigration):
    changed: int

    def report(self) -> int:
        return self.changed


@dataclass(frozen=True)
class PreviousWatch:
    """The saved command before its base directory became a declared field."""

    bus: str
    root: str
    command: tuple[str, ...]

    @classmethod
    def read(cls, text: str) -> PreviousWatch:
        payload = json.loads(text)
        declaration = (
            RecordedWatch if FieldCodec.record_key(WatchRecord, "base_state") in payload else cls
        )
        return FieldCodec.decode(declaration, payload)

    def base(self, derived: Path) -> Path:
        return derived

    def recorded_bases(self) -> tuple[Path, ...]:
        return ()

    def arguments(self) -> tuple[argparse.Namespace, list[str]]:
        parser = argparse.ArgumentParser(add_help=False)
        parser.add_argument("--state", type=Path, action="append", default=[])
        parser.add_argument("--session", action="append", default=[])
        parser.add_argument("--bus-thread", action="append", default=[])
        return parser.parse_known_args(self.command[2:])

    def default_base(self, home: Path) -> Path:
        (entry,) = entry_points(group="console_scripts", name=Path(self.command[0]).name)
        (bridge,) = (
            bridge
            for bridge in (ClaudeBridge, CodexBridge, OpenCodeBridge)
            if bridge.__module__ == entry.module
        )
        return home / bridge.state_directory


@dataclass(frozen=True)
class RecordedWatch(WatchRecord, PreviousWatch):
    """An already-carried command retains its declared base on repeat or revert."""

    def base(self, derived: Path) -> Path:
        return self.base_state

    def recorded_bases(self) -> tuple[Path, ...]:
        return (self.base_state,)


def default_bases(home: Path) -> tuple[Path, ...]:
    return tuple(
        home / bridge.state_directory for bridge in (ClaudeBridge, CodexBridge, OpenCodeBridge)
    )


@dataclass(frozen=True)
class WatchRecordFile:
    path: Path
    record: WatchRecord

    def held(self) -> bool:
        with self.path.open() as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_SH | fcntl.LOCK_NB)
            except BlockingIOError:
                return True
        return False

    def directories(self) -> tuple[WatchDirectory, ...]:
        return ()

    def write(self, record: WatchRecord) -> None:
        with self.path.open("r+") as lock:
            lock.write(record.to_json())
            lock.truncate()
            lock.flush()


@dataclass(frozen=True)
class WatchDirectory(WatchRecordFile):
    state: Path
    session: str

    def directories(self) -> tuple[WatchDirectory, ...]:
        return (self,)

    def moved(self, state: Path) -> WatchRecord:
        _, remaining = PreviousWatch(
            self.record.bus, self.record.root, self.record.command
        ).arguments()
        return replace(
            self.record,
            command=(
                *self.record.command[:2],
                *remaining,
                "--state",
                str(state),
            ),
            base_state=state.parent,
        )


def recorded_watches(home: Path) -> tuple[WatchRecordFile, ...]:
    """Decode old commands only here; new runtime readers require the typed base_state."""
    watches: list[WatchRecordFile] = []
    for path in sorted(WatchClaim.directory(home).glob("*.lock")):
        text = path.read_text()
        if not text:
            continue
        previous = PreviousWatch.read(text)
        arguments, _ = previous.arguments()
        for base in arguments.state or (previous.default_base(home),):
            record = WatchRecord(previous.bus, previous.root, previous.command, previous.base(base))
            candidates = (
                base.joinpath(*arguments.session),
                base.joinpath(*arguments.bus_thread),
                base,
            )
            existing = tuple(
                state
                for state in dict.fromkeys(candidates)
                if (state / BridgeStateTable.filename).is_file()
            )
            if not existing:
                print(f"Watch journal missing: {path}: {base}")
                watches.append(WatchRecordFile(path, record))
                continue
            state = existing[0]
            with closing(
                sqlite3.connect((state / BridgeStateTable.filename).as_uri() + "?mode=ro", uri=True)
            ) as db:
                identities = PreviousBinding.read_binding(db)
                if not identities and DeliveryRow.count(db):
                    raise ValueError(f"Journal rows without a binding: {state}")
            if not identities:
                watches.append(WatchRecordFile(path, record))
                continue
            (identity,) = identities
            base_state = base
            if state.name in (identity.thread, identity.bus):
                base_state = state.parent
            record = replace(record, base_state=previous.base(base_state))
            if (identity.root, identity.bus) != (record.root, record.bus):
                if arguments.state:
                    raise ValueError(f"Watch and binding disagree: {path}, {state}")
                continue
            watches.append(WatchDirectory(path, record, state, identity.thread))
    return tuple(watches)


def state_directories(home: Path, watches: tuple[WatchRecordFile, ...]) -> tuple[Path, ...]:
    """Recorded commands retain custom state paths; default roots retain inactive bindings."""
    states = {directory.state for watch in watches for directory in watch.directories()}
    for base in default_bases(home):
        states.update(
            path.parent
            for path in base.rglob(BridgeStateTable.filename)
            if ".binding-start-backup" not in path.parts
        )
    return tuple(sorted(states))


def migrate(state: Path) -> BindingMigration:
    """Carry the binding table atomically, preserving every journal and observation row."""
    path = state / BridgeStateTable.filename
    if not path.is_file():
        raise FileNotFoundError(path)
    with closing(SQLiteFile.writer(path)) as db, SQLiteFile.write_transaction(db):
        owner, = Binding._fields_named(("owner",))
        if owner.wire_name in Binding.stored_columns(db):
            return MigratedBindings(0)
        previous = [row.registered(db) for row in PreviousBinding.read_binding(db)]
        Binding.drop(db)
        Binding.create(db)
        for binding in previous:
            binding.insert(db)
    return MigratedBindings(len(previous))


@dataclass(frozen=True)
class SavedJournal:
    state: Annotated[Path, PathText]
    saved: Annotated[Path, PathText]

    def save(self) -> None:
        self.saved.mkdir(parents=True)
        for path in self.state.glob(BridgeStateTable.filename + "*"):
            shutil.copy2(path, self.saved / path.name)

    def restore(self) -> None:
        self.state.mkdir(parents=True, exist_ok=True)
        for path in self.state.glob(BridgeStateTable.filename + "*"):
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
        with self.path.open("r+") as lock:
            lock.write(self.saved.read_text())
            lock.truncate()
            lock.flush()


@dataclass(frozen=True)
class DirectoryBackup:
    journals: tuple[SavedJournal, ...]
    watches: tuple[SavedWatch, ...]
    destinations: tuple[Annotated[Path, PathText], ...]
    root: Annotated[Path, PathText]

    def save(self, path: Path) -> None:
        for journal in self.journals:
            journal.save()
        for watch in self.watches:
            watch.save()
        path.write_text(FieldCodec.compact(FieldCodec.encode(self)))

    def restore(self) -> int:
        for destination in self.destinations:
            for path in destination.glob(BridgeStateTable.filename + "*"):
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
            raise ValueError(f"Different payloads for delivery {self.id}")
        return issubclass(self.outcome, FilteredOutcome) and not issubclass(
            previous.outcome, FilteredOutcome
        )


@dataclass(frozen=True)
class ReaderDirectories:
    identity: BindingIdentity
    states: tuple[Path, ...]
    watches: tuple[WatchDirectory, ...]

    def selected_watch(self) -> WatchDirectory:
        live = tuple(watch for watch in self.watches if watch.held())
        sessions = {watch.session for watch in live}
        if len(sessions) != 1:
            raise ValueError(f"Expected one live transport for {self.identity.bus}: {sessions}")
        return live[0]

    @property
    def destination(self) -> Path:
        return self.selected_watch().record.base_state / self.identity.bus

    def merge(self) -> None:
        bindings: dict[Path, Binding] = {}
        for state in self.states:
            migrate(state).report()
            with Binding.open(state) as db:
                (binding,) = Binding.select(db)
                bindings[state] = binding
        position = max(
            (binding.start for binding in bindings.values()), key=lambda start: start.cursor()
        )
        registry = RegistrySnapshot.capture(
            RegistryStore(RegistryStore.path_in(Path(self.identity.root))).read()
        )
        commander = registry.commander_project.commanders(registry)
        reader = registry.require(self.identity.bus)
        if any(reader.incarnation == thread.incarnation for thread in commander):
            head = Binding.bus_head(self.identity.root)
            print(
                f"{self.identity.bus}: start {position.cursor()} -> {head.cursor()}; "
                f"skipped {head.cursor() - position.cursor()} bus positions"
            )
            position = head
        selected = self.selected_watch()
        destination = self.destination
        merged = destination.parent / ".binding-start-backup" / (self.identity.bus + ".sqlite3")
        with Binding.open(merged.parent / (self.identity.bus + ".merging")) as target:
            with SQLiteFile.write_transaction(target):
                deliveries: dict[str, JournalDelivery] = {}
                for state in self.states:
                    with closing(
                        sqlite3.connect(
                            (state / BridgeStateTable.filename).as_uri() + "?mode=ro", uri=True
                        )
                    ) as source:
                        for table in BridgeStateTable.schema_rows():
                            if table is Binding:
                                continue
                            if table is ObservedPart:
                                if bindings[state].thread != selected.session:
                                    continue
                                for part in ObservedPart.select(source):
                                    if (
                                        not part.insert_if_absent(target).rowcount
                                        and ObservedPart.require(
                                            target, id=part.id, number=part.number
                                        )
                                        != part
                                    ):
                                        raise ValueError(f"Conflicting observation in {state}")
                                continue
                            columns = table._column_list(tuple(table.column_projection()))
                            for values in source.execute(
                                f'SELECT {columns} FROM "{table.declared_name}"'
                            ):
                                if table is DeliveryRow:
                                    (incoming,) = DeliveryRow.select(
                                        source, row_type=JournalDelivery, id=values[0]
                                    )
                                    if incoming.id in deliveries:
                                        if not incoming.preferred_to(deliveries[incoming.id]):
                                            continue
                                        DeliveryRow.delete(target, id=incoming.id)
                                    deliveries[incoming.id] = incoming
                                target.execute(table.insertion_statement(), values)
                Binding(root=self.identity.root, owner=self.identity.owner,
                        thread=selected.session, start=position).insert(
                    target
                )
            destination.mkdir(parents=True, exist_ok=True)
            with closing(SQLiteFile.writer(merged)) as output:
                target.backup(output)
        for state in self.states:
            for path in state.glob(BridgeStateTable.filename + "*"):
                path.unlink()
        shutil.copy2(merged, destination / BridgeStateTable.filename)
        for state in self.states:
            if state != destination and not tuple(state.iterdir()):
                state.rmdir()
        for watch in self.watches:
            watch.write(watch.moved(destination))
        print(
            f"{self.identity.bus}: merged {len(self.states)} stores, "
            f"{len(deliveries)} deliveries -> {destination}; session {selected.session}; "
            f"start {position.cursor()}"
        )


def merge_directories(home: Path, root: Path) -> int:
    watches = recorded_watches(home)
    groups: dict[tuple[str, ThreadIncarnation], list[Path]] = {}
    identities = {}
    unbound = []
    for state in state_directories(home, watches):
        with closing(
            sqlite3.connect((state / BridgeStateTable.filename).as_uri() + "?mode=ro", uri=True)
        ) as db:
            bindings = [row.registered(db) for row in PreviousBinding.read_binding(db)]
            if not bindings and DeliveryRow.count(db):
                raise ValueError(f"Journal rows without a binding: {state}")
        if not bindings:
            unbound.append(state)
            continue
        identity, = bindings
        key = (identity.root, identity.owner)
        identities[key] = identity
        groups.setdefault(key, []).append(state)
    readers = tuple(
        ReaderDirectories(
            identities[key],
            tuple(states),
            tuple(
                directory
                for watch in watches
                for directory in watch.directories()
                if directory.record.root == key[0]
                and key[1].matches_recorded_name(
                    directory.record.bus,
                    RegistrySnapshot.capture(
                        RegistryStore(RegistryStore.path_in(Path(key[0]))).read()),
                )
            ),
        )
        for key, states in groups.items()
    )
    by_base: dict[Path, list[ReaderDirectories]] = {}
    for reader in readers:
        if reader.identity.root != str(root):
            for state in reader.states:
                print(f"left in place: {reader.identity.bus} at {state} (other root)")
            continue
        if not any(watch.held() for watch in reader.watches):
            for state in reader.states:
                print(f"left in place: {reader.identity.bus} at {state} (no live reader)")
            continue
        by_base.setdefault(reader.destination.parent, []).append(reader)
    for watch in watches:
        if watch.record.root == str(root):
            by_base.setdefault(watch.record.base_state, [])
    changed = 0
    for base, members in by_base.items():
        directory = base / ".binding-start-backup"
        manifest = directory / "manifest.json"
        if manifest.is_file():
            print(f"Already merged: {base}")
            continue
        empty = (
            tuple(state for state in unbound if state == base or state.parent == base)
            if members
            else ()
        )
        sources = tuple(
            dict.fromkeys((*(state for member in members for state in member.states), *empty))
        )
        records = tuple(
            watch
            for watch in watches
            if watch.record.root == str(root) and watch.record.base_state == base
        )
        commands = tuple(dict.fromkeys(watch.path for watch in records))
        backup = DirectoryBackup(
            tuple(
                SavedJournal(state, directory / "journals" / str(index))
                for index, state in enumerate(sources)
            ),
            tuple(SavedWatch(path, directory / "watches" / path.name) for path in commands),
            tuple(member.destination for member in members),
            root,
        )
        backup.save(manifest)
        for watch in records:
            watch.write(watch.record)
        for state in empty:
            for path in state.glob(BridgeStateTable.filename + "*"):
                path.unlink()
            print(f"Deleted empty unbound journal: {state}")
        for member in members:
            member.merge()
            changed += len(member.states)
    return changed


def revert(home: Path, root: Path) -> int:
    # A failed forward run may already have removed an old journal. Discover its
    # backup from the saved command without opening the journal being restored.
    bases = set(default_bases(home))
    for path in WatchClaim.directory(home).glob("*.lock"):
        text = path.read_text()
        if not text:
            continue
        previous = PreviousWatch.read(text)
        arguments, _ = previous.arguments()
        for state in arguments.state:
            bases.update((state, state.parent))
        bases.update(previous.recorded_bases())
    changed = 0
    for base in sorted(bases):
        directory = base / ".binding-start-backup"
        manifest = directory / "manifest.json"
        if manifest.is_file():
            backup = FieldCodec.decode(DirectoryBackup, json.loads(manifest.read_text()))
            if backup.root != root:
                print(f"left backup: {directory} (other root {backup.root})")
                continue
            changed += backup.restore()
            shutil.rmtree(directory)
    return changed


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--home", type=Path, required=True)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument(
        "--revert", dest="operation", action="store_const", const=revert, default=merge_directories
    )
    arguments = parser.parse_args()
    changed = arguments.operation(arguments.home, arguments.root)
    print(f"Binding rows changed: {changed}")


if __name__ == "__main__":
    main()
