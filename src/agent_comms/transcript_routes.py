"""Routing annotations keyed by durable Pi entry IDs, never reply text."""

import hashlib
import json
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .declarations import RelationViolationError, TurnRouting, _store_lock, file_revision


@dataclass(frozen=True, slots=True)
class InputDisplay:
    """Owner-authored presentation for one native input; None means internal."""

    text: str | None
    routing: TurnRouting | None = None
    sent_text_digest: str | None = None

    def matches(self, text: str) -> bool:
        return (
            self.sent_text_digest is None
            or self.sent_text_digest == hashlib.sha256(text.encode("utf-8")).hexdigest()
        )


class _SessionRoutes:
    """One indexed read snapshot; decode only entries used by the page."""

    def __init__(self, path: Path | None, session_file: str):
        self._connection = sqlite3.connect(path, timeout=30) if path else None
        if self._connection is not None:
            # Keep all annotations on one page at one committed revision.
            self._connection.execute("BEGIN")
        self._session_file = session_file
        self._cache: dict[str, TurnRouting | None] = {}
        self._input_cache: dict[str, InputDisplay | None] = {}

    def get(self, entry_id: Any, default: None = None) -> TurnRouting | None:
        if not isinstance(entry_id, str) or self._connection is None:
            return default
        if entry_id not in self._cache:
            row = self._connection.execute(
                "SELECT route FROM routes WHERE session_file = ? AND entry_id = ?",
                (self._session_file, entry_id),
            ).fetchone()
            self._cache[entry_id] = TurnRouting.from_wire(json.loads(row[0])) if row else None
        return self._cache[entry_id]

    def input_display(self, native_id: Any) -> InputDisplay | None:
        if not isinstance(native_id, str) or self._connection is None:
            return None
        if native_id not in self._input_cache:
            row = self._connection.execute(
                "SELECT display_text, routing, sent_text_digest FROM input_display "
                "LEFT JOIN input_routing USING(native_id) WHERE native_id = ?",
                (native_id,),
            ).fetchone()
            self._input_cache[native_id] = (
                InputDisplay(
                    row[0], TurnRouting.from_wire(json.loads(row[1])) if row[1] else None, row[2]
                )
                if row
                else None
            )
        return self._input_cache[native_id]

    def close(self) -> None:
        if self._connection is not None:
            self._connection.close()
            self._connection = None

    def __enter__(self) -> "_SessionRoutes":
        return self

    def __exit__(self, *_error: object) -> None:
        self.close()


class TranscriptRoutes:
    def __init__(self, path: Path):
        self.path = path  # Legacy JSON source, imported when its revision changes.
        self.database_path = path.with_suffix(".sqlite3")
        self._initialized = False
        self._legacy_revision: tuple[int, int, int, int] | None = None

    def _ensure_database(self, *, create: bool = False) -> bool:
        revision = file_revision(self.path)
        if self._initialized and self.database_path.is_file() and revision == self._legacy_revision:
            return True
        if not create and not self.database_path.exists() and revision is None:
            return False
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        with (
            _store_lock(self.path),
            closing(sqlite3.connect(self.database_path, timeout=30)) as connection,
        ):
            connection.execute("PRAGMA synchronous=FULL")
            connection.execute(
                "CREATE TABLE IF NOT EXISTS routes ("
                "session_file TEXT NOT NULL, entry_id TEXT NOT NULL, route TEXT NOT NULL, "
                "source TEXT NOT NULL, "
                "PRIMARY KEY (session_file, entry_id)) WITHOUT ROWID"
            )
            connection.execute(
                "CREATE TABLE IF NOT EXISTS metadata " "(key TEXT PRIMARY KEY, value TEXT NOT NULL)"
            )
            connection.execute(
                "CREATE TABLE IF NOT EXISTS input_display "
                "(native_id TEXT PRIMARY KEY, display_text TEXT) WITHOUT ROWID"
            )
            # Keep the two-column display table compatible with running older
            # writers that use INSERT ... VALUES without a column list.
            connection.execute(
                "CREATE TABLE IF NOT EXISTS input_routing "
                "(native_id TEXT PRIMARY KEY, sent_text_digest TEXT NOT NULL, routing TEXT) "
                "WITHOUT ROWID"
            )
            # An older process can keep writing the JSON map during a rolling
            # upgrade. Import only when its authoritative file revision moves.
            revision = file_revision(self.path)
            recorded = connection.execute(
                "SELECT value FROM metadata WHERE key = 'legacy_revision'"
            ).fetchone()
            if recorded is None or recorded[0] != json.dumps(revision):
                raw = json.loads(self.path.read_text()) if self.path.exists() else {}
                if not isinstance(raw, dict):
                    raise ValueError("Legacy transcript routes are not an object.")
                with connection:
                    for session_file, entries in raw.items():
                        if not isinstance(session_file, str) or not isinstance(entries, dict):
                            raise ValueError("Legacy transcript route session is malformed.")
                        for entry_id, route in entries.items():
                            if not isinstance(entry_id, str):
                                raise ValueError("Legacy transcript route entry is malformed.")
                            canonical = TurnRouting.from_wire(route).to_wire()
                            connection.execute(
                                "INSERT INTO routes VALUES (?, ?, ?, 'legacy') "
                                "ON CONFLICT(session_file, entry_id) DO UPDATE "
                                "SET route = excluded.route WHERE routes.source = 'legacy'",
                                (session_file, entry_id, json.dumps(canonical)),
                            )
                    connection.execute(
                        "INSERT OR REPLACE INTO metadata VALUES ('legacy_revision', ?)",
                        (json.dumps(revision),),
                    )
        self._initialized = True
        self._legacy_revision = revision
        return True

    def for_session(self, session_file: str) -> _SessionRoutes:
        path = self.database_path if self._ensure_database() else None
        return _SessionRoutes(path, session_file)

    def input_bindings(self) -> dict[str, tuple[str, str | None]]:
        """Read existing bindings without creating a database or upgrading its schema."""
        if not self.database_path.exists():
            return {}
        with closing(
            sqlite3.connect(self.database_path.resolve().as_uri() + "?mode=ro", uri=True)
        ) as connection:
            if (
                connection.execute(
                    "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'input_routing'"
                ).fetchone()
                is None
            ):
                return {}
            return {
                native_id: (digest, routing)
                for native_id, digest, routing in connection.execute(
                    "SELECT native_id, sent_text_digest, routing FROM input_routing"
                )
            }

    def record(self, session_file: str, entry_ids: tuple[str, ...], routing: TurnRouting) -> None:
        if not entry_ids:
            return
        self._ensure_database(create=True)
        encoded = json.dumps(routing.to_wire())
        with (
            _store_lock(self.path),
            closing(sqlite3.connect(self.database_path, timeout=30)) as connection,
        ):
            connection.execute("PRAGMA synchronous=FULL")
            with connection:
                connection.executemany(
                    "INSERT OR REPLACE INTO routes VALUES (?, ?, ?, 'indexed')",
                    ((session_file, entry_id, encoded) for entry_id in entry_ids),
                )

    def record_input_display(
        self,
        native_id: str,
        display_text: str | None,
        *,
        sent_text: str | None = None,
        routing: TurnRouting | None = None,
    ) -> None:
        """Persist before prompt write, including before Pi creates its session file."""
        if routing is not None and (sent_text is None or routing.reply is not None):
            raise ValueError("Input routing requires exact sent text and no outgoing reply claim.")
        digest = (
            hashlib.sha256(sent_text.encode("utf-8")).hexdigest() if sent_text is not None else None
        )
        encoded = json.dumps(routing.to_wire(), sort_keys=True) if routing is not None else None
        self._ensure_database(create=True)
        with (
            _store_lock(self.path),
            closing(sqlite3.connect(self.database_path, timeout=30)) as connection,
        ):
            connection.execute("PRAGMA synchronous=FULL")
            with connection:
                if digest is not None:
                    previous = connection.execute(
                        "SELECT sent_text_digest, routing FROM input_routing WHERE native_id = ?",
                        (native_id,),
                    ).fetchone()
                    if previous is not None and previous != (digest, encoded):
                        raise RelationViolationError("Native input routing cannot be rebound.")
                    connection.execute(
                        "INSERT OR IGNORE INTO input_routing VALUES (?, ?, ?)",
                        (native_id, digest, encoded),
                    )
                connection.execute(
                    "INSERT OR IGNORE INTO input_display (native_id, display_text) VALUES (?, ?)",
                    (native_id, display_text),
                )
