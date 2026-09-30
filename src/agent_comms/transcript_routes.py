"""Routing annotations keyed by durable Pi entry IDs, never reply text."""

import hashlib
import os
import sqlite3
from contextlib import closing
from dataclasses import dataclass, field, replace
from pathlib import Path

from .errors import RelationViolationError
from .routing import TurnRouting
from .store_files import _store_lock, file_revision
from .typed_table import Column, SQLiteSchemaObject, TypedTable


class RouteAnnotationTable:
    """Durable annotations written by an owner, not a transcript-derived cache."""


@dataclass(frozen=True)
class TranscriptRoute(RouteAnnotationTable, TypedTable):
    session_file: str = field(metadata={"sql": Column(primary_key=True)})
    entry_id: str = field(metadata={"sql": Column(primary_key=True)})
    routing: TurnRouting
    without_rowid = True

    def require_publication(self, routing: TurnRouting) -> None:
        if self.routing.publications and self.routing != routing:
            raise RelationViolationError("Original native publication cannot be rebound")


@dataclass(frozen=True)
class InputDisplay(RouteAnnotationTable, TypedTable):
    """Owner-authored presentation for one native input; None text means internal."""

    native_id: str = field(metadata={"sql": Column(primary_key=True)})
    text: str | None
    routing: TurnRouting | None = None
    sent_text_digest: str | None = None
    without_rowid = True
    checks = ("routing IS NULL OR sent_text_digest IS NOT NULL",)

    def matches(self, text: str) -> bool:
        return (
            self.sent_text_digest is None
            or self.sent_text_digest == hashlib.sha256(text.encode("utf-8")).hexdigest()
        )


def _schema(connection: sqlite3.Connection) -> dict[str, str]:
    return {
        row.name: row.sql
        for row in SQLiteSchemaObject.read(
            connection.execute("SELECT name, sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'")
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
        raise ValueError("Transcript annotations require the one-shot durable migration")


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
            self._input_cache[native_id] = InputDisplay.one(self._connection, native_id=native_id)
        return self._input_cache[native_id]

    def close(self) -> None:
        if self._connection is not None:
            self._connection.close()
            self._connection = None

    def __enter__(self) -> "_SessionRoutes":
        return self

    def __exit__(self, *_error: object) -> None:
        self.close()


@dataclass(frozen=True, slots=True)
class TranscriptRouteRevision:
    database: tuple[int, int, int, int] | None
    journal: tuple[int, int, int, int] | None


class TranscriptRoutes:
    filename = "transcript_routes.sqlite3"

    def __init__(self, root: Path):
        self.database_path = root / self.filename
        self._initialized = False

    def revision(self) -> TranscriptRouteRevision:
        """SQLite owns its committed data in the database or its WAL file."""
        return TranscriptRouteRevision(
            file_revision(self.database_path),
            file_revision(self.database_path.with_name(self.database_path.name + "-wal")),
        )

    def snapshot(self, destination: Path) -> None:
        """Retain a consistent current annotation store for attached history."""
        if not self.database_path.exists():
            return
        target = destination / self.filename
        descriptor = os.open(target, os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o600)
        os.close(descriptor)
        with (
            closing(
                sqlite3.connect(self.database_path.resolve().as_uri() + "?mode=ro", uri=True)
            ) as source,
            closing(sqlite3.connect(target)) as output,
        ):
            _assert_schema(source)
            source.backup(output)
            _assert_schema(output)

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

    def record_turn_publication(self, registry, log, lease, checkpoint, routing, published):
        """Join a completed native producer to its original committed wire rows.

        Native traversal closes before taking source custody. Existing wire,
        registry and input owners remain held until durable annotations commit.
        A partial successful send subset remains published if a later send fails.
        """
        from .bus_publication import stable_thread_lookup
        from .input_disposition import InputDispositions
        from .native_transcript import NativeTranscript
        from .native_input_owner import RegistryOwner
        from .private_bus_checkpoint import conversation_sources_unlocked

        if not routing.requires_annotation(published):
            return
        initial = registry.snapshot()
        incarnation = lease.identity.incarnation.resolved(initial)
        thread = RegistryOwner.capture_local(initial, incarnation.name).thread
        if thread.turn_lease != lease.renamed(incarnation.name):
            raise RelationViolationError("Native publication has no current original turn")
        path = Path(thread.require_saved_session())
        revision = file_revision(path)
        if revision is None:
            raise RelationViolationError("Native publication has no retained session")
        reader = NativeTranscript(path)
        floor = checkpoint.offset if str(path) == checkpoint.session_file else 0
        try:
            reader.require_session_id()
            final, user = reader.publication_input(after=floor, through=revision[1])
        except (ValueError, TypeError) as error:
            raise RelationViolationError("Native publication evidence is invalid") from error
        ids = routing.annotation_ids(reader, checkpoint, str(path), final.end)
        inputs = InputDispositions(log.path.parent / InputDispositions.filename)
        lookup = stable_thread_lookup(incarnation.created_at)
        with log.certified_read() as source, registry.store.reading() as document, inputs.reading() as attempts:
            snapshot = document.snapshot()
            incarnation = lease.identity.incarnation.resolved(snapshot)
            current = RegistryOwner.capture_local(snapshot, incarnation.name).thread
            if current.turn_lease != lease.renamed(incarnation.name):
                raise RelationViolationError("Native publication turn lease changed")
            if current.session_file != str(path) or file_revision(path) != revision:
                raise RelationViolationError("Native publication session changed")
            if not attempts.started_for_native(lease, user.input_id, user.message.text, snapshot=snapshot):
                raise RelationViolationError("Native publication has no original STARTED input")
            references = tuple(dict.fromkeys((*routing.requests, *published)))
            marks = ",".join("?" for _ in references)
            originals = conversation_sources_unlocked(
                source, lookup, f"w.seq IN ({marks})", tuple(ref.seq for ref in references),
                limit=len(references), ascending=True,
            ) if references else ()
            if {item.message.reference for item in originals} != set(references) or len(set(published)) != len(published):
                raise RelationViolationError("Publication is not its original seq/id relation")
            routing.require_publications(
                tuple(item for item in originals if item.message.reference in published), lookup
            )
            self._record_publication(str(path), ids, final.entry.id, routing, published)

    def _record_publication(self, session_file, entry_ids, final_id, routing, published):
        self._ensure_database(create=True)
        with _store_lock(self.database_path), closing(sqlite3.connect(self.database_path, timeout=30)) as connection:
            connection.execute("PRAGMA synchronous=FULL")
            with connection:
                final_routing = replace(routing, publications=published)
                previous = TranscriptRoute.one(connection, session_file=session_file, entry_id=final_id)
                if previous is not None:
                    previous.require_publication(final_routing)
                for entry_id in entry_ids:
                    if entry_id == final_id:
                        continue
                    TranscriptRoute(session_file, entry_id, routing).upsert(connection)
                TranscriptRoute(session_file, final_id, final_routing).upsert(connection)

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
