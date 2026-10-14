"""One-time carry: record each native context commit time where it is written.

Run with the NEW runtime's interpreter, with every owner stopped:
    $R/bin/python -I tools/cutover/context_commit_time.py <wire root> <backup dir>

Before this change the time an input was shown to the model was re-derived on
every read by decoding the recipient's whole native session. It now lives on the
native proof journal row (schema 2, written by the native writer) and on
Core's native_runtime_input row (a new native runtime DDL digest). Existing data has no recorded commit time, so
this carry takes it once from the best recorded fact:

- A generation's commit time is the latest session-entry timestamp among its
  inputs (the context was assembled after all of them were recorded).
- A Core row whose journal is current takes its generation's time; a row whose
  journal is the retired JSONL format (already refused at runtime) takes its own
  entry's timestamp.
- A Core row whose native session file was deleted takes the creation time of
  the attempt that delivered it (a triage row: its session's full attempt).

Journals are rebuilt privately, fsynced, backed up and atomically replaced;
native_runtime_input is rebuilt in one IMMEDIATE transaction and checked
against the declared native runtime schema. Delete this tool after the cutover.
"""

import json
import os
import shutil
import sqlite3
import sys
from collections import defaultdict
from contextlib import closing
from datetime import datetime
from pathlib import Path

from agent_comms.coordinated_runtime_schema import _digest, _schema, assert_native_runtime_schema
from agent_comms.native_pi import NativeContextJournal
from agent_comms.native_runtime_input import NativeRuntimeInput, NativeRuntimeSchemaMeta

JOURNAL = NativeContextJournal.declared_name
INPUTS = NativeRuntimeInput.declared_name


def entry_times(session_file: Path, entry_ids: set[str]) -> dict[str, int]:
    found = {}
    with open(session_file, "rb") as source:
        for line in source:
            if not any(entry.encode() in line for entry in entry_ids):
                continue
            row = json.loads(line)
            if row.get("id") in entry_ids:
                found[row["id"]] = int(datetime.fromisoformat(row["timestamp"]).timestamp() * 1000)
    missing = entry_ids - found.keys()
    if missing:
        raise SystemExit(f"{session_file}: entries without timestamps {sorted(missing)[:3]}")
    return found


def is_sqlite(path: Path) -> bool:
    with open(path, "rb") as source:
        return source.read(16) == b"SQLite format 3\x00"


def fsync_path(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def carry_journal(proof: Path, backups: Path) -> dict[int, int]:
    """Rebuild one current journal at schema 2; return generation -> commit time."""
    session_file = Path(str(proof).removesuffix(".input-proof"))
    old = sqlite3.connect(f"file:{proof}?mode=ro", uri=True)
    columns = ("input_id", "session_id", "session_entry_id", "request_generation", "llm_context_digest")
    rows = old.execute(f"SELECT {','.join(columns)} FROM {JOURNAL} ORDER BY request_generation").fetchall()
    old.close()
    times = entry_times(session_file, {row[2] for row in rows})
    generations: dict[int, int] = defaultdict(int)
    for row in rows:
        generations[row[3]] = max(generations[row[3]], times[row[2]])
    staged = proof.with_name(proof.name + ".carry")
    staged.unlink(missing_ok=True)
    os.close(os.open(staged, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600))
    new = sqlite3.connect(staged, isolation_level=None)
    try:
        new.execute("PRAGMA journal_mode=DELETE")
        new.execute("BEGIN IMMEDIATE")
        for ddl in NativeContextJournal.schema_objects().values():
            new.execute(ddl)
        insert = NativeContextJournal.native_contract()["insert"]
        wire = [column["name"] for column in NativeContextJournal.native_contract()["columns"]]
        for row in rows:
            values = dict(zip(columns, row), committed_at_ms=generations[row[3]],
                          schema=2, type="context_committed")
            new.execute(insert, [values[name] for name in wire])
        if new.execute(f"SELECT count(*) FROM {JOURNAL}").fetchone()[0] != len(rows):
            raise SystemExit(f"{proof}: row count changed")
        new.execute("COMMIT")
        if new.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise SystemExit(f"{proof}: integrity_check failed")
    finally:
        new.close()
    fsync_path(staged)
    backup = backups / proof.relative_to(proof.anchor)
    backup.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(proof, backup)
    fsync_path(backup)
    os.replace(staged, proof)
    fsync_path(proof.parent)
    return dict(generations)


def attempt_times(db: sqlite3.Connection) -> dict[str, int]:
    """Creation time of each execution's first attempt, from Core's own record."""
    return dict(db.execute(
        "SELECT execution_id, MIN(created_at_ms) FROM attempts GROUP BY execution_id"
    ).fetchall())


def input_times(db: sqlite3.Connection, journal_times: dict[Path, dict[int, int]]) -> dict[str, int]:
    rows = db.execute(
        f"SELECT input_id, stage, execution_id, session_file, session_entry_id, request_generation "
        f"FROM {INPUTS} WHERE session_entry_id IS NOT NULL"
    ).fetchall()
    attempts = attempt_times(db)
    by_session: dict[str, list] = defaultdict(list)
    for row in rows:
        by_session[row[3]].append(row)
    result = {}
    for session_file, members in by_session.items():
        session = Path(session_file)
        if not session.exists():
            # The native session was deleted: Core's own delivery attempt time.
            full = [attempts[row[2]] for row in members if row[2] is not None]
            for input_id, _stage, execution_id, *_ in members:
                result[input_id] = attempts[execution_id] if execution_id is not None else min(full)
            continue
        generations = journal_times.get(Path(f"{session_file}.input-proof"))
        own = None
        for input_id, _stage, _execution, _file, entry_id, generation in members:
            if generations is not None and generation in generations:
                result[input_id] = generations[generation]
                continue
            if own is None:
                own = entry_times(session, {row[4] for row in members})
            result[input_id] = own[entry_id]
    return result


def carry_coordination(path: Path, times: dict[str, int]) -> int:
    """Rebuild native_runtime_input with the new column and install the new DDL digest."""
    target = NativeRuntimeInput.schema_objects()
    meta_table = NativeRuntimeSchemaMeta.declared_name
    db = sqlite3.connect(path, isolation_level=None)
    db.execute("PRAGMA foreign_keys=OFF")
    db.execute("PRAGMA legacy_alter_table=ON")
    db.execute("BEGIN IMMEDIATE")
    try:
        dependents = db.execute(
            "SELECT type, name FROM sqlite_master WHERE tbl_name=? AND type='trigger'", (INPUTS,)
        ).fetchall()
        for kind, name in dependents:
            db.execute(f'DROP {kind.upper()} "{name}"')
        count = db.execute(f"SELECT count(*) FROM {INPUTS}").fetchone()[0]
        db.execute(f'ALTER TABLE "{INPUTS}" RENAME TO "{INPUTS}__carry_old"')
        db.execute(target[INPUTS])
        db.execute("CREATE TEMP TABLE carry_time (input_id TEXT PRIMARY KEY, committed_at_ms INTEGER NOT NULL)")
        db.executemany("INSERT INTO temp.carry_time VALUES (?,?)", times.items())
        old_columns = [row[1] for row in db.execute(f'PRAGMA table_xinfo("{INPUTS}__carry_old")') if row[6] == 0]
        listed = ", ".join(f'o."{column}"' for column in old_columns)
        names = ", ".join(f'"{column}"' for column in old_columns)
        db.execute(
            f'INSERT INTO "{INPUTS}" ({names}, committed_at_ms) SELECT {listed}, t.committed_at_ms '
            f'FROM "{INPUTS}__carry_old" o LEFT JOIN temp.carry_time t ON t.input_id=o.input_id'
        )
        db.execute(f'DROP TABLE "{INPUTS}__carry_old"')
        if db.execute(f"SELECT count(*) FROM {INPUTS}").fetchone()[0] != count:
            raise SystemExit("native_runtime_input row count changed")
        for name, sql in target.items():
            if name != INPUTS:
                db.execute(sql)
        guard = f"{meta_table}_update_guard"
        guard_sql = NativeRuntimeSchemaMeta.schema_objects()[guard]
        db.execute(f'DROP TRIGGER "{guard}"')
        db.execute(f"UPDATE {meta_table} SET ddl_digest=? WHERE singleton=1", (_digest(_schema()),))
        db.execute(guard_sql)
        if db.execute("PRAGMA foreign_key_check").fetchall():
            raise SystemExit("foreign key violations")
        if db.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise SystemExit("integrity_check failed")
        db.execute("COMMIT")
    except BaseException:
        db.execute("ROLLBACK")
        raise
    finally:
        db.close()
    check = sqlite3.connect(path)
    try:
        check.execute("PRAGMA foreign_keys=ON")
        assert_native_runtime_schema(check)
    finally:
        check.close()
    return count


def main(root: str, backups: str) -> None:
    root_path, backup_path = Path(root), Path(backups)
    database = root_path / "coordination.sqlite3"
    reader = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
    sessions = {Path(row[0]) for row in reader.execute(
        f"SELECT DISTINCT session_file FROM {INPUTS} WHERE session_file IS NOT NULL")}
    reader.close()
    proofs = sorted({Path(f"{session}.input-proof") for session in sessions} |
                    set(root_path.rglob("*.input-proof")))
    journal_times, retired = {}, []
    for proof in proofs:
        if not proof.exists():
            continue
        if not is_sqlite(proof):
            retired.append(proof)
            continue
        journal_times[proof] = carry_journal(proof, backup_path / "journals")
    source = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
    with closing(sqlite3.connect(backup_path / "coordination.sqlite3")) as copy:
        source.backup(copy)
    source.close()
    reader = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
    times = input_times(reader, journal_times)
    reader.close()
    rows = carry_coordination(database, times)
    print({"journals": len(journal_times), "retired_jsonl_journals": len(retired),
           "inputs_timed": len(times), "input_rows": rows})


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
