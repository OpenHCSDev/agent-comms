"""Rewrite retained annotations once into current tables; delete after activation."""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path

from agent_comms.field_codec import FieldCodec
from agent_comms.routing import TurnRouting
from agent_comms.store_files import _atomic_write_text, file_revision
from agent_comms.transcript_events import TranscriptCodec
from agent_comms.transcript_routes import InputDisplay, TranscriptRoute, TranscriptRoutes


@dataclass(frozen=True)
class AnnotationRewrite:
    source: str
    staged: str
    routes: int
    inputs: int
    json_overrides: int


def stage(source: Path, destination: Path) -> AnnotationRewrite:
    source, destination = source.resolve(), destination.absolute()
    if destination == source or destination.is_relative_to(source):
        raise ValueError("Annotation stage must be separate from original history")
    database = source / TranscriptRoutes.filename
    saved_json = source / "transcript_routes.json"
    before = tuple(file_revision(path) for path in (database, saved_json))
    destination.mkdir(mode=0o700, parents=False, exist_ok=False)
    json_text = saved_json.read_text() if saved_json.exists() else None
    if json_text is not None:
        _atomic_write_text(destination / "original-transcript-routes.json", json_text)
    archived = destination / "original-transcript-routes.sqlite3"
    if database.exists():
        with (
            closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)) as original,
            closing(sqlite3.connect(archived)) as backup,
        ):
            original.backup(backup)
            if backup.execute("PRAGMA integrity_check").fetchone() != ("ok",):
                raise ValueError(f"Invalid annotation source: {database}")
        os.chmod(archived, 0o600)
    owner = TranscriptRoutes(destination)
    owner._ensure_database(create=True)
    count = inputs = overrides = 0
    # JSON is the older bounded snapshot. SQLite rows stream throughout; the
    # complete indexed history is never materialized into a second Python store.
    pending = (
        TranscriptCodec.decode(dict[str, dict[str, TurnRouting]], json.loads(json_text))
        if json_text is not None
        else {}
    )
    with closing(sqlite3.connect(owner.database_path)) as output:
        output.execute("PRAGMA synchronous=FULL")
        with output:
            if archived.exists():
                with closing(sqlite3.connect(archived.as_uri() + "?mode=ro", uri=True)) as old:
                    names = {
                        row[0]
                        for row in old.execute("SELECT name FROM sqlite_master WHERE type='table'")
                    }
                    if (
                        "metadata" in names
                        and old.execute(
                            "SELECT value FROM metadata WHERE key='routes_imported'"
                        ).fetchone()
                        is not None
                    ):
                        pending = {}
                    if "routes" in names:
                        has_source = any(
                            row[1] == "source" for row in old.execute("PRAGMA table_info(routes)")
                        )
                        # Old source provenance decides precedence only here.
                        for row in old.execute(
                            "SELECT session_file,entry_id,route,"
                            + ("source" if has_source else "NULL")
                            + " FROM routes"
                        ):
                            session, entry, raw, provenance = row
                            replacement = pending.get(session, {}).pop(entry, None)
                            routing = TranscriptCodec.decode(TurnRouting, json.loads(raw))
                            if replacement is not None and provenance == "legacy":
                                routing = replacement
                                overrides += 1
                            TranscriptRoute(session, entry, routing).insert(output)
                            count += 1
                    if "input_routing" in names and (
                        "input_display" not in names
                        or old.execute(
                            "SELECT 1 FROM input_routing "
                            "LEFT JOIN input_display USING(native_id) "
                            "WHERE input_display.native_id IS NULL LIMIT 1"
                        ).fetchone()
                        is not None
                    ):
                        raise ValueError(
                            f"Orphan input routing in {database}; preserve and investigate"
                        )
                    if "input_display" in names:
                        query = (
                            "SELECT native_id,display_text AS text,routing,sent_text_digest "
                            "FROM input_display LEFT JOIN input_routing USING(native_id)"
                            if "input_routing" in names
                            else "SELECT native_id,display_text AS text,NULL AS routing,"
                            "NULL AS sent_text_digest FROM input_display"
                        )
                        for row in InputDisplay.iterate(old.execute(query)):
                            row.insert(output)
                            inputs += 1
            for session, entries in pending.items():
                for entry, routing in entries.items():
                    TranscriptRoute(session, entry, routing).insert(output)
                    count += 1
    # Reopen via the current owner and compare every current input binding and
    # route through typed reads. The separate acceptance compares source values.
    reopened = TranscriptRoutes(destination)
    reopened._ensure_database()
    with closing(sqlite3.connect(reopened.database_path)) as output:
        actual_routes = sum(
            1 for _ in TranscriptRoute.iterate(output.execute("SELECT * FROM transcript_route"))
        )
        actual_inputs = sum(
            1 for _ in InputDisplay.iterate(output.execute("SELECT * FROM input_display"))
        )
        if (actual_routes, actual_inputs) != (count, inputs):
            raise ValueError("Reopened annotation counts differ from preserved rows")
        if output.execute("PRAGMA integrity_check").fetchone() != ("ok",):
            raise ValueError("Reopened annotations failed SQLite integrity check")
    if before != tuple(file_revision(path) for path in (database, saved_json)):
        raise ValueError("Annotation source changed during staging; candidate is not installable")
    receipt = AnnotationRewrite(str(source), str(destination), count, inputs, overrides)
    _atomic_write_text(
        destination / "annotation-rewrite.json", json.dumps(FieldCodec.encode(receipt), indent=2)
    )
    return receipt


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("destination", type=Path)
    args = parser.parse_args()
    print(json.dumps(FieldCodec.encode(stage(args.source, args.destination)), indent=2))
