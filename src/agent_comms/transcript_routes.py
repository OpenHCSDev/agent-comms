"""Routing annotations keyed by durable Pi entry IDs, never reply text."""

import hashlib
import os
import sqlite3
from collections.abc import Iterator
from contextlib import closing, contextmanager
from dataclasses import dataclass, field, replace
from pathlib import Path

from .field_codec import MalformedRecord
from .coordination_database import CoordinationStore
from .errors import RelationViolationError
from .private_path import FileRevision
from .routing import TurnRouting
from .store_files import _store_lock
from .typed_table import Column, DurableTables, StoreWriter, TypedTable


class RouteAnnotationTable(DurableTables):
    """Annotations written by an owner, not a transcript-derived cache."""

    schema_error = ValueError


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


class _SessionRoutes:
    """One indexed read snapshot; decode only entries used by the page."""

    def __init__(self, path: Path | None, session_file: str):
        self._connection = CoordinationStore.read_snapshot(path) if path else None
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


class TranscriptRoutes(StoreWriter):
    filename = "transcript_routes.sqlite3"

    def __init__(self, root: Path):
        super().__init__(self.path_in(root))
        self._initialized = False

    def revision(self) -> FileRevision | None:
        return FileRevision.capture(self.source_paths(self.path.parent)[0])

    def snapshot(self, destination: Path) -> None:
        """Retain a consistent current annotation store for attached history."""
        if not self.path.exists():
            return
        target = self.path_in(destination)
        descriptor = os.open(target, os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o600)
        os.close(descriptor)
        with (
            closing(CoordinationStore.read_snapshot(self.path)) as source,
            closing(CoordinationStore.writer(target)) as output,
        ):
            RouteAnnotationTable.require_schema(source)
            source.backup(output)
            RouteAnnotationTable.require_schema(output)

    def _ensure_database(self, *, create: bool = False) -> bool:
        if self._initialized and self.path.is_file():
            return True
        if not create and not self.path.exists():
            return False
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with (
            _store_lock(self.path),
            closing(CoordinationStore.writer(self.path)) as connection,
        ):
            connection.execute("PRAGMA synchronous=FULL")
            with self.transaction(connection):
                if not RouteAnnotationTable.stored_schema(connection):
                    RouteAnnotationTable.create_schema(connection)
                RouteAnnotationTable.reconcile_schema(connection)
        self._initialized = True
        return True

    @contextmanager
    def writing(self) -> Iterator[sqlite3.Connection]:
        self._ensure_database(create=True)
        with (
            _store_lock(self.path),
            closing(CoordinationStore.writer(self.path)) as connection,
        ):
            connection.execute("PRAGMA synchronous=FULL")
            with self.transaction(connection):
                yield connection

    def for_session(self, session_file: str) -> _SessionRoutes:
        path = self.path if self._ensure_database() else None
        return _SessionRoutes(path, session_file)

    def input_bindings(self) -> dict[str, InputDisplay]:
        """Read typed owner bindings without creating or upgrading a database."""
        if not self.path.exists():
            return {}
        with closing(CoordinationStore.read_snapshot(self.path)) as connection:
            RouteAnnotationTable.require_schema(connection)
            return {
                row.native_id: row
                for row in InputDisplay.select(connection, where="sent_text_digest IS NOT NULL")
            }

    def record(self, session_file: str, entry_ids: tuple[str, ...], routing: TurnRouting) -> None:
        if not entry_ids:
            return
        with self.writing() as connection:
            for entry_id in entry_ids:
                TranscriptRoute(session_file, entry_id, routing).upsert(connection)

    def record_turn_publication(self, registry, log, lease, checkpoint, routing, published):
        """Join a completed native producer to its original committed wire rows.

        Finish reading native history before locking the sources. Existing wire,
        registry and input owners remain held until durable annotations commit.
        A partial successful send subset remains published if a later send fails.
        """
        from .input_disposition import InputDispositions
        from .native_transcript import NativeTranscript
        from .native_input_owner import RegistryOwner

        if not routing.requires_annotation(published):
            return
        initial = registry.snapshot()
        incarnation = lease.identity.incarnation.resolved(initial)
        thread = RegistryOwner.capture_local(initial, incarnation.name).thread
        if thread.turn_lease != lease:
            raise RelationViolationError("Native publication has no current original turn")
        path = Path(thread.runtime.path)
        revision = FileRevision.capture(path)
        if revision is None:
            raise RelationViolationError("Native publication has no retained session")
        reader = NativeTranscript(path)
        floor = checkpoint.offset if thread.runtime.read_key == checkpoint.session_file else 0
        try:
            final, user = reader.publication_input(after=floor, through=revision.size)
        except MalformedRecord as error:
            raise RelationViolationError("Native publication evidence is invalid") from error
        ids = routing.annotation_ids(reader, checkpoint, thread.runtime, final.end)
        inputs = InputDispositions(log.path.parent)
        lookup = incarnation.lookup
        with log.checked_read() as source, registry.store.reading_snapshot() as snapshot:
            incarnation = lease.identity.incarnation.resolved(snapshot)
            current = RegistryOwner.capture_local(snapshot, incarnation.name).thread
            if current.turn_lease != lease:
                raise RelationViolationError("Native publication turn lease changed")
            if current.runtime.read_key != thread.runtime.read_key or FileRevision.capture(path) != revision:
                raise RelationViolationError("Native publication session changed")
            if not inputs.in_turn(lease.turn_id).started_for_native(
                    lease, user.input_id, user.message.text, snapshot=snapshot):
                raise RelationViolationError("Native publication has no original STARTED input")
            references = tuple(dict.fromkeys((*routing.requests, *published)))
            marks = ",".join("?" for _ in references)
            originals = tuple(source.conversation_sources(
                lookup, f"w.seq IN ({marks})", tuple(ref.seq for ref in references),
                limit=len(references), ascending=True,
            )) if references else ()
            if {item.message.reference for item in originals} != set(references) or len(set(published)) != len(published):
                raise RelationViolationError("Publication is not its original seq/id relation")
            routing.require_publications(
                tuple(item for item in originals if item.message.reference in published), lookup
            )
            self._record_publication(thread.runtime.read_key, ids, final.entry.id, routing, published)

    def _record_publication(self, session_file, entry_ids, final_id, routing, published):
        with self.writing() as connection:
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
        with self.writing() as connection:
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
