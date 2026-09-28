"""Compare every saved routing annotation across the one-way R6 migration.

Only copied databases are migrated. SQLite backup captures the read-only source;
comparison streams sorted rows so historical transcripts never accumulate in RAM.
"""
import itertools
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
from contextlib import closing


def backup(source, target):
    with closing(sqlite3.connect(source.resolve().as_uri() + "?mode=ro", uri=True)) as a:
        with closing(sqlite3.connect(target)) as b:
            a.backup(b)


def migrate(root):
    from agent_comms.transcript_routes import TranscriptRoutes

    TranscriptRoutes(root)._ensure_database(create=True)


def run(base, candidate):
    from agent_comms.store_files import _store_lock
    from agent_comms.transcript_routes import TranscriptRoutes

    roots = (
        Path("/home/ts/.agent-comms"),
        Path("/var/tmp/agent-comms-live-20260927-wzjtqhza"),
        Path("/var/tmp/agent-comms-live-20260927-6_d_vdul"),
    )
    base.mkdir(parents=True, exist_ok=False)
    report = {"equal": True, "roots": [], "live_writes": 0}
    for index, source in enumerate(roots):
        old, new = base / str(index) / "old", base / str(index) / "new"
        old.mkdir(parents=True)
        new.mkdir()
        legacy = source / "transcript_routes.json"
        if legacy.exists():
            with _store_lock(legacy, shared=True):
                shutil.copyfile(legacy, old / legacy.name)
            shutil.copyfile(old / legacy.name, new / legacy.name)
        database = source / "transcript_routes.sqlite3"
        if database.exists():
            backup(database, old / database.name)
        TranscriptRoutes(old / legacy.name)._ensure_database(create=True)
        backup(old / database.name, new / database.name)
        env = dict(os.environ)
        env["PYTHONPATH"] = str(candidate / "src")
        subprocess.run([sys.executable, __file__, "migrate", str(new)], env=env,
                       check=True, timeout=60)
        item = {"source": str(source), "equal": True, "tables": {}}
        with closing(sqlite3.connect(old / database.name)) as before:
            with closing(sqlite3.connect(new / database.name)) as after:
                queries = {
                    "routes": ("SELECT session_file,entry_id,route FROM routes ORDER BY session_file,entry_id", 2),
                    "input_display": ("SELECT native_id,display_text FROM input_display ORDER BY native_id", None),
                    "input_routing": ("SELECT native_id,sent_text_digest,routing FROM input_routing ORDER BY native_id", 2),
                }
                for name, (sql, json_column) in queries.items():
                    count, different = 0, 0
                    for a, b in itertools.zip_longest(before.execute(sql), after.execute(sql)):
                        count += 1
                        if json_column is not None and a is not None and b is not None:
                            a, b = list(a), list(b)
                            a[json_column] = json.loads(a[json_column]) if a[json_column] else None
                            b[json_column] = json.loads(b[json_column]) if b[json_column] else None
                        different += a != b
                    item["tables"][name] = {"rows": count, "differences": different}
                    item["equal"] &= different == 0
                item["new_columns"] = [r[1] for r in after.execute("PRAGMA table_info(routes)")]
                item["migration_marker"] = after.execute(
                    "SELECT value FROM metadata WHERE key='routes_imported'"
                ).fetchone()
                assert "source" not in item["new_columns"] and item["migration_marker"] == ("1",)
        # Reopening must use the canonical database without consulting old JSON.
        if (new / legacy.name).exists():
            (new / legacy.name).write_text('{"invalid after completed migration": true}')
        subprocess.run([sys.executable, __file__, "migrate", str(new)], env=env,
                       check=True, timeout=60)
        item["reopen_uses_canonical_database"] = True
        report["roots"].append(item)
        report["equal"] &= item["equal"]
    print(json.dumps(report, indent=2))
    if not report["equal"]:
        raise SystemExit(1)


if __name__ == "__main__":
    if sys.argv[1] == "migrate":
        migrate(Path(sys.argv[2]))
    else:
        run(Path(sys.argv[1]), Path(sys.argv[2]))
