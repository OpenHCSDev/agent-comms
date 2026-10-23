"""Drop copied thread names after the installer stops the old owners.

The declared tables own their replacement schema and retained columns. Retire
this step after the retained buses have been installed with it.
"""

from __future__ import annotations

import sqlite3
from contextlib import ExitStack, closing
from pathlib import Path

from agent_comms.cohort_schema import AwarenessClaimGenerations
from agent_comms.coordination_database import CoordinationStore
from agent_comms.coordination_tables.attempts import AttemptRecord
from agent_comms.coordination_tables.executions import ExecutionRecord
from agent_comms.coordination_tables.participants import OwnerGenerations, Participants
from agent_comms.errors import RelationViolationError
from agent_comms.installer.carry import Carried, Carry
from agent_comms.locked_store import SQLiteFile
from agent_comms.native_prompt_binding import PromptBinding
from agent_comms.native_runtime_input import CurrentNativeCursor, NativeRuntimeInput
from agent_comms.private_sidecar import (
    SnapshotMeta, _connect, _digest, _locked_directory, _publish, _read_snapshot, _verify_schema,
)
from agent_comms.store_files import _store_lock
from agent_comms.typed_table import HeldBusFile, SQLiteCatalogObject, SQLiteSchemaObject


class ThreadNameOwners(Carry):
    def backup_files(self) -> tuple[Path, ...]:
        paths = (CoordinationStore.path_in(self.root), PromptBinding.path_in(self.root))
        for path in paths:
            self.require_confined(self.root, path)
        return tuple(path for path in paths if path.exists())

    def carry(self, *, apply: bool) -> Carried:
        paths = self.backup_files()
        intent = self.root / ".private-owner-rename.pending"
        if intent.exists() or intent.is_symlink():
            raise RelationViolationError("Inspect the unfinished old rename before carrying its bus.")
        changed = 0
        detail: dict[str, int] = {}
        rewritten: list[Path] = []
        for path in paths:
            with ExitStack() as lifetime:
                sidecar = path.name == PromptBinding.filename
                if sidecar:
                    directory = lifetime.enter_context(_locked_directory(path))
                    snapshot = _read_snapshot(directory, path.name)
                    if snapshot is None:
                        raise RelationViolationError("Prompt binding disappeared before its carry.")
                    raw, identity = snapshot
                    db = lifetime.enter_context(closing(_connect(raw)))
                else:
                    lifetime.enter_context(_store_lock(path))
                    db = lifetime.enter_context(closing(SQLiteFile.writer(":memory:", isolation_level=None)))
                    # The snapshot answers one copy; held longer, it blocks the write-back below.
                    with closing(SQLiteFile.read_snapshot(path)) as source:
                        source.backup(db)
                tables = (
                    (Participants, OwnerGenerations, ExecutionRecord, AttemptRecord,
                     NativeRuntimeInput, CurrentNativeCursor, AwarenessClaimGenerations)
                    if path.name == CoordinationStore.filename else (PromptBinding,)
                )
                present = tuple(table for table in tables if table.stored_columns(db))
                pending = tuple(table for table in present if SQLiteSchemaObject.differences(
                    table.stored_objects(db), table.schema_objects()
                ))
                aliases = db.execute(
                    "SELECT count(*) FROM sqlite_master WHERE type='table' AND name='participant_aliases'"
                ).fetchone()[0]
                if not pending and not aliases:
                    continue
                before = {
                    table.declared_name: db.execute(
                        f'SELECT count(*) FROM "{table.declared_name}"'
                    ).fetchone()[0]
                    for table in present
                }
                # Rebuild preserves existing lifecycle rows. Insert guards are
                # for new transitions, not for replaying sealed historical rows.
                db.execute("PRAGMA foreign_keys=OFF")
                db.setconfig(sqlite3.SQLITE_DBCONFIG_ENABLE_TRIGGER, False)
                with SQLiteFile.write_transaction(db):
                    SQLiteCatalogObject.carry(
                        db, {}, frozenset(name for table in pending for name in table.schema_objects())
                    )
                    for table in pending:
                        table.rebuild(db)
                    if aliases:
                        db.execute("DROP TABLE participant_aliases")
                    if sidecar:
                        SnapshotMeta.update(db, where="singleton=1", ddl_digest=_digest(PromptBinding))
                db.setconfig(sqlite3.SQLITE_DBCONFIG_ENABLE_TRIGGER, True)
                db.execute("PRAGMA foreign_keys=ON")
                for table in present:
                    if SQLiteSchemaObject.differences(table.stored_objects(db), table.schema_objects()):
                        raise RelationViolationError(f"Carried {table.declared_name} schema differs.")
                    after = db.execute(f'SELECT count(*) FROM "{table.declared_name}"').fetchone()[0]
                    if before[table.declared_name] != after:
                        raise RelationViolationError(f"Carry changed {table.declared_name} row count.")
                    detail[f"{table.declared_name}_before"] = before[table.declared_name]
                    detail[f"{table.declared_name}_after"] = after
                    for _ in table.stream(db):
                        pass
                if list(db.execute("PRAGMA foreign_key_check")):
                    raise RelationViolationError("Carried owner tables have broken foreign keys.")
                if sidecar:
                    _verify_schema(db, PromptBinding)
                changed += len(pending) + aliases
                if apply:
                    owner = CoordinationStore if path.name == CoordinationStore.filename else PromptBinding
                    HeldBusFile(owner, path).require()
                    if sidecar:
                        _publish(directory, path.name, identity, db.serialize())
                    else:
                        with closing(SQLiteFile.writer(path)) as destination:
                            db.backup(destination)
                    rewritten.append(path.relative_to(self.root))
        return Carried(changed, detail, tuple(rewritten))
