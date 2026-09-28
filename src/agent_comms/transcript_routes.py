"""Routing annotations keyed by durable Pi entry IDs, never reply text."""

import hashlib
import sqlite3
from contextlib import closing
from dataclasses import dataclass, field
from pathlib import Path

from .errors import RelationViolationError
from .routing import TurnRouting
from .store_files import _store_lock
from .transcript_events import TranscriptCodec
from .typed_table import Column, JsonStorage, TypedRow, TypedTable


class RouteAnnotationTable:
    """Durable annotations written by an owner, not a transcript-derived cache."""


class TranscriptRoutingStorage(JsonStorage):
    codec = TranscriptCodec

    @classmethod
    def accepts(cls, annotation: object) -> bool:
        return False  # The routing field explicitly declares its existing boundary codec.


@dataclass(frozen=True)
class TranscriptRoute(RouteAnnotationTable, TypedTable):
    session_file: str = field(metadata={"sql": Column(primary_key=True)})
    entry_id: str = field(metadata={"sql": Column(primary_key=True)})
    routing: TurnRouting = field(metadata={"sql": Column(storage=TranscriptRoutingStorage)})
    without_rowid = True


@dataclass(frozen=True)
class InputDisplay(RouteAnnotationTable, TypedTable):
    """Owner-authored presentation for one native input; None text means internal."""

    native_id: str = field(metadata={"sql": Column(primary_key=True)})
    text: str | None
    routing: TurnRouting | None = field(
        default=None, metadata={"sql": Column(storage=TranscriptRoutingStorage)}
    )
    sent_text_digest: str | None = None
    without_rowid = True
    checks = ("routing IS NULL OR sent_text_digest IS NOT NULL",)

    def matches(self, text: str) -> bool:
        return (
            self.sent_text_digest is None
            or self.sent_text_digest == hashlib.sha256(text.encode("utf-8")).hexdigest()
        )


@dataclass(frozen=True)
class _AnnotationSchema(TypedRow):
    name: str
    sql: str


def _schema(connection: sqlite3.Connection) -> dict[str, str]:
    return {
        row.name: row.sql
        for row in _AnnotationSchema.read(
            connection.execute(
                "SELECT name, sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'"
            )
        )
    }


def _declared_schema() -> dict[str, str]:
    return {
        name: sql
        for table in TypedTable.members_with(RouteAnnotationTable)
        for name, sql in table.schema_objects().items()
    }


def _assert_schema(connection: sqlite3.Connection) -> None:
    if _schema(connection) != _declared_schema():
        raise ValueError("Transcript annotations require the one-shot durable cutover")


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

    def get(self, entry_id: str | None) -> TurnRouting | None:
        if entry_id is None or self._connection is None:
            return None
        if entry_id not in self._cache:
            row = TranscriptRoute.one(
                self._connection, session_file=self._session_file, entry_id=entry_id
            )
            self._cache[entry_id] = row.routing if row else None
        return self._cache[entry_id]

    def input_display(self, native_id: str | None) -> InputDisplay | None:
        if native_id is None or self._connection is None:
            return None
        if native_id not in self._input_cache:
            self._input_cache[native_id] = InputDisplay.one(
                self._connection, native_id=native_id
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
    filename = "transcript_routes.sqlite3"

    def __init__(self, root: Path):
        self.database_path = root / self.filename
        self._initialized = False

    def _ensure_database(self, *, create: bool = False) -> bool:
        if self._initialized and self.database_path.is_file():
            return True
        if not create and not self.database_path.exists():
            return False
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        with (
            _store_lock(self.database_path),
            closing(sqlite3.connect(self.database_path, timeout=30)) as connection,
        ):
            connection.execute("PRAGMA synchronous=FULL")
            with connection:
                connection.execute("BEGIN IMMEDIATE")
                if not _schema(connection):
                    for table in TypedTable.members_with(RouteAnnotationTable):
                        table.create(connection)
                _assert_schema(connection)
        self._initialized = True
        return True

    def for_session(self, session_file: str) -> _SessionRoutes:
        path = self.database_path if self._ensure_database() else None
        return _SessionRoutes(path, session_file)

    def input_bindings(self) -> dict[str, InputDisplay]:
        """Read typed owner bindings without creating or upgrading a database."""
        if not self.database_path.exists():
            return {}
        with closing(
            sqlite3.connect(self.database_path.resolve().as_uri() + "?mode=ro", uri=True)
        ) as connection:
            _assert_schema(connection)
            return {
                row.native_id: row
                for row in InputDisplay.select(connection, where="sent_text_digest IS NOT NULL")
            }

    def record(self, session_file: str, entry_ids: tuple[str, ...], routing: TurnRouting) -> None:
        if not entry_ids:
            return
        self._ensure_database(create=True)
        with (
            _store_lock(self.database_path),
            closing(sqlite3.connect(self.database_path, timeout=30)) as connection,
        ):
            connection.execute("PRAGMA synchronous=FULL")
            with connection:
                for entry_id in entry_ids:
                    TranscriptRoute(session_file, entry_id, routing).upsert(connection)

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
        self._ensure_database(create=True)
        with (
            _store_lock(self.database_path),
            closing(sqlite3.connect(self.database_path, timeout=30)) as connection,
        ):
            connection.execute("PRAGMA synchronous=FULL")
            with connection:
                # Serialize the read/first-write boundary across processes and threads.
                connection.execute("BEGIN IMMEDIATE")
                previous = InputDisplay.one(connection, native_id=native_id)
                if previous is None:
                    InputDisplay(native_id, display_text, routing, digest).insert(connection)
                elif digest is not None:
                    if previous.sent_text_digest is not None:
                        if (previous.sent_text_digest, previous.routing) != (digest, routing):
                            raise RelationViolationError("Native input routing cannot be rebound.")
                    else:
                        InputDisplay.update(
                            connection,
                            where="native_id=?",
                            parameters=(native_id,),
                            routing=routing,
                            sent_text_digest=digest,
                        )
