"""Give retained claim fields their declared names, once, under install maintenance.

The old spelling is selected by the record declaration, never by the JSON value.
Opaque provider JSON is not a record and is never searched for matching strings.
Delete this step after every retained root has crossed this format change.
"""

from __future__ import annotations

import json
from contextlib import closing
from pathlib import Path

from agent_comms.errors import RelationViolationError
from agent_comms.field_codec import FieldCodec, compile_decoder
from agent_comms.goal_failure_observation import FailedTurnEvidence
from agent_comms.historical_views import HistoryArchive
from agent_comms.installer.carry import Carried, Carry
from agent_comms.locked_store import JsonDocument, RegistryFile, RootFile, SQLiteFile
from agent_comms.registry_document import RegistryDocument
from agent_comms.registry_store import RegistryStore
from agent_comms.restart_queue import DIRECTORY, QueuedRestart
from agent_comms.selected_write_plan import (
    SelectedWriteIntent,
    SelectedWritePlans,
)
from agent_comms.store_files import AtomicFile, _store_lock
from agent_comms.typed_table import HeldBusFile, JsonStorage, TypedTable, _identifier

from .carry_bus_history import CarryCatalogSource
from .carry_context_request_key import ContextRequestKey
from .carry_dead_turns import DeadTurns
from .carry_finished_turns import FinishedTurns
from .carry_owner_requests import OwnerRequests
from .carry_turn_owner import TurnOwner
from .retired_claim_fields import RetiredClaimFields


class ClaimFields(RetiredClaimFields, Carry):
    precedes = (FinishedTurns, DeadTurns, OwnerRequests, TurnOwner, ContextRequestKey)

    @staticmethod
    def documents(root: Path, files: dict[Path, type[RootFile]]) -> dict[Path, object]:
        documents = {path: owner.record_type for path, owner in files.items()
                     if issubclass(owner, JsonDocument) and not issubclass(owner, RegistryStore)}
        documents.update((path, SelectedWriteIntent) for path in SelectedWritePlans.matching_in(
            root / SelectedWritePlans.root_directory))
        documents.update((path, QueuedRestart) for path in (root / DIRECTORY).rglob("*.json"))
        return documents

    def roots(self) -> tuple[Path, ...]:
        return (self.root, *(source.mapped_root(self.root, self.original_root)
                             for source in CarryCatalogSource.read(self.root, self.original_root)))

    def backup_files(self) -> tuple[Path, ...]:
        paths = set()
        for root in self.roots():
            files = RootFile.existing_in(root)
            paths.update(path for path, owner in files.items() if issubclass(owner, SQLiteFile))
            paths.update(self.documents(root, files))
            paths.update(RegistryStore.at(root).written)
        paths.add(HistoryArchive.path_in(self.root))
        for path in paths:
            self.require_confined(self.root, path)
        return tuple(sorted(path for path in paths if path.exists()))

    def carry(self, *, apply: bool) -> Carried:
        if apply:
            HeldBusFile(RegistryFile, RegistryFile.path_in(self.root)).require()
        rewritten = []
        changed = 0
        with _store_lock(HistoryArchive.path_in(self.root)):
            sources = CarryCatalogSource.read(self.root, self.original_root)
            for root in self.roots():
                registry = RegistryStore.at(root)
                if registry.path.exists():
                    with registry.locked():
                        registry.private_guard_unlocked()
                        raw = json.loads(registry.path.read_bytes())
                        current = self.convert(RegistryDocument, raw)
                        if current != raw:
                            changed += 1
                            if apply:
                                registry._write_unlocked(json.dumps(current, indent=2))
                                rewritten.extend(str(path.relative_to(self.root))
                                                 for path in registry.written)
                files = RootFile.existing_in(root)
                for path, owner in files.items():
                    self.require_confined(self.root, path)
                    if issubclass(owner, SQLiteFile):
                        count = self.database(path, apply=apply)
                        changed += count
                        if count and apply:
                            rewritten.append(str(path.relative_to(self.root)))
                for path, declared in self.documents(root, files).items():
                    self.require_confined(self.root, path)
                    plan = compile_decoder(declared)
                    if not plan.contains_records(self.selects_record):
                        continue
                    with _store_lock(path):
                        raw = json.loads(path.read_bytes())
                        current = plan.rewrite_document(raw, self.rename_record, self.selects_record)
                        count = int(current != raw)
                        if count and apply:
                            AtomicFile(path).write_text(json.dumps(current))
                    changed += count
                    if count and apply:
                        rewritten.append(str(path.relative_to(self.root)))
            if apply and rewritten:
                rebound = tuple(source.carried_registry(source.mapped_root(self.root, self.original_root))
                                for source in sources)
                if rebound:
                    AtomicFile(HistoryArchive.path_in(self.root)).write_text(
                        json.dumps(FieldCodec.encode(rebound)))
                    rewritten.append(HistoryArchive.filename)
        return Carried(changed, rewritten=tuple(rewritten))

    @classmethod
    def database(cls, path: Path, *, apply: bool) -> int:
        # Read only typed JSON columns that can contain these declarations. The
        # multi-gigabyte message bodies are neither copied into RAM nor decoded.
        pending = []
        rebuilds = []
        changed = 0
        with closing(SQLiteFile.read_snapshot(path)) as db:
            tables = tuple(row for row in TypedTable.members_with(TypedTable)
                           if row.stored_columns(db))
            for row in tables:
                stored = row.stored_columns(db)
                renamed = {field.name: _identifier(old)
                           for field in row._fields()
                           if field.column.record_key
                           and (old := cls.retired_fields(row).get(field.name)) in stored}
                if any(field.wire_name in stored for field in row._fields()
                       if field.name in renamed):
                    raise RelationViolationError(f"{row.__name__} contains old and new claim columns")
                json_fields = tuple(field for field in row._fields()
                                    if issubclass(field.storage, JsonStorage) and field.wire_name in stored
                                    and field.value.contains_records(cls.selects_record))
                primary = tuple(field for field in row._fields() if field.column.primary_key)
                if json_fields:
                    selected = (*primary, *json_fields)
                    columns = ",".join(renamed.get(field.name, _identifier(field.wire_name))
                                       for field in selected)
                    where = " AND ".join(
                        f"{renamed.get(field.name, _identifier(field.wire_name))} IS ?"
                        for field in primary)
                    if not primary:
                        raise RelationViolationError(f"{row.__name__} has no declared row key")
                    with closing(db.execute(f"SELECT {columns} FROM {_identifier(row.declared_name)}")) as rows:
                        for values in rows:
                            keys, payloads = values[:len(primary)], values[len(primary):]
                            updates = {}
                            for field, value in zip(json_fields, payloads, strict=True):
                                if value is None:
                                    continue
                                raw = json.loads(value)
                                current = field.value.rewrite_records(raw, cls.rename_record, cls.selects_record)
                                if current != raw:
                                    updates[field.wire_name] = json.dumps(current)
                            if updates:
                                assignments = ",".join(f"{_identifier(key)}=?" for key in updates)
                                updates_for_row = (f"UPDATE {_identifier(row.declared_name)} SET {assignments} WHERE {where}",
                                                   (*updates.values(), *keys))
                                # Keep only changed rows, not the scanned source.
                                pending.append(updates_for_row)
                                changed += 1
                if renamed:
                    rebuilds.append((row, renamed))
                    changed += 1
        if not apply or not changed:
            return changed
        with _store_lock(path), closing(SQLiteFile.writer(path, isolation_level=None)) as db:
            triggers = db.execute("SELECT name,sql FROM sqlite_master WHERE type='trigger'").fetchall()
            db.execute("PRAGMA foreign_keys=OFF")
            with SQLiteFile.write_transaction(db):
                for name, _ in triggers:
                    db.execute(f"DROP TRIGGER {_identifier(name)}")
                for query, arguments in pending:
                    db.execute(query, arguments)
                for row, renamed in rebuilds:
                    row.rebuild(db, renamed)
                present = {name for (name,) in db.execute("SELECT name FROM sqlite_master WHERE type='trigger'")}
                for name, sql in triggers:
                    if name not in present:
                        db.execute(sql)
                if problems := db.execute("PRAGMA foreign_key_check").fetchall():
                    raise RelationViolationError(f"Claim carry broke references in {path}: {problems}")
            return changed
