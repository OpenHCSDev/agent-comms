"""Routing annotations keyed by durable Pi entry IDs, never reply text."""

import hashlib
import os
from contextlib import ExitStack, closing
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Annotated, ClassVar

from agent_comms.native_session_files import NativeSessionFiles
from agent_comms.native_session_reopen import NativeSessionIdentity

from .errors import RelationViolationError
from .field_codec import MalformedRecord
from .private_path import FileObservation, FileRevision
from .routing import TurnRouting
from .typed_table import Column, DurableStore, TypedTable, WithoutRowid


@dataclass(frozen=True)
class TranscriptRoute(WithoutRowid):
    session_file: Annotated[str, Column(primary_key=True)]
    entry_id: Annotated[str, Column(primary_key=True)]
    routing: TurnRouting

    def require_publication(self, routing: TurnRouting) -> None:
        if self.routing.publications and self.routing != routing:
            raise RelationViolationError("Original native publication cannot be rebound")


@dataclass(frozen=True)
class InputDisplay(WithoutRowid):
    """Owner-authored presentation for one native input; None text means internal."""

    native_id: Annotated[str, Column(primary_key=True)]
    text: str | None
    routing: TurnRouting | None = None
    sent_text_digest: str | None = None
    checks = ("routing IS NULL OR sent_text_digest IS NOT NULL",)

    def matches(self, text: str) -> bool:
        return (
            self.sent_text_digest is None
            or self.sent_text_digest == hashlib.sha256(text.encode("utf-8")).hexdigest()
        )


class _SessionRoutes:
    """One indexed read snapshot; decode only entries used by the page."""

    def __init__(self, store: TranscriptRoutes, session_file: str):
        self._lifetime = ExitStack()
        self._connection = self._lifetime.enter_context(store.snapshot())
        self._session_file = session_file
        self._cache: dict[str, TurnRouting | None] = {}
        self._input_cache: dict[str, InputDisplay | None] = {}

    def get(self, entry_id: str | None) -> TurnRouting | None:
        if entry_id is None:
            return None
        if entry_id not in self._cache:
            row = TranscriptRoute.one(
                self._connection, session_file=self._session_file, entry_id=entry_id
            )
            self._cache[entry_id] = row.routing if row else None
        return self._cache[entry_id]

    def input_display(self, native_id: str | None) -> InputDisplay | None:
        if native_id is None:
            return None
        if native_id not in self._input_cache:
            self._input_cache[native_id] = InputDisplay.one(self._connection, native_id=native_id)
        return self._input_cache[native_id]

    def close(self) -> None:
        self._lifetime.close()

    def __enter__(self) -> "_SessionRoutes":
        return self

    def __exit__(self, *_error: object) -> None:
        self.close()


@dataclass(frozen=True, slots=True)
class TranscriptRoutes(DurableStore):
    """Annotations written by an owner, not a transcript-derived cache."""

    root: Path
    filename: ClassVar[str] = "transcript_routes.sqlite3"
    rows: ClassVar[tuple[type[TypedTable], ...]] = (TranscriptRoute, InputDisplay)

    def revision(self) -> FileObservation:
        return FileRevision.capture(self.source_paths(self.root)[0])

    def retain(self, destination: Path) -> None:
        """Retain a consistent current annotation store for attached history."""
        if not self.path.exists():
            return
        target = self.path_in(destination)
        descriptor = os.open(target, os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o600)
        os.close(descriptor)
        with self.snapshot() as source, closing(self.writer(target)) as output:
            source.backup(output)
            self.require_schema(output)

    def for_session(self, session_file: str) -> _SessionRoutes:
        return _SessionRoutes(self, session_file)

    def input_bindings(self) -> dict[str, InputDisplay]:
        with self.snapshot() as connection:
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
        from .native_input_owner import RegistryOwner
        from .native_transcript import NativeTranscript

        if not routing.requires_annotation(published):
            return
        initial = registry.snapshot()
        incarnation = lease.identity.incarnation.resolved(initial)
        thread = RegistryOwner.capture_local(initial, incarnation.name).thread
        if thread.turn_lease != lease:
            raise RelationViolationError("Native publication has no current original turn")
        path = Path(thread.runtime.path)
        revision = FileRevision.capture(path).require(
            RelationViolationError("Native publication has no retained session"))
        reader = NativeTranscript(path)
        floor = checkpoint.offset if thread.runtime.read_key == checkpoint.session_file else 0
        try:
            final, user = reader.publication_input(
                after=floor, through=revision.size,
                files=NativeSessionFiles.for_identity(NativeSessionIdentity.locate(str(path)),
                                                      root=log.path.parent, owner=incarnation),
            )
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
            if (
                current.runtime.read_key != thread.runtime.read_key
                or FileRevision.capture(path) != revision
            ):
                raise RelationViolationError("Native publication session changed")
            if not inputs.in_turn(lease.turn_id).started_for_native(
                lease, user.input_id, user.message.text, snapshot=snapshot
            ):
                raise RelationViolationError("Native publication has no original STARTED input")
            references = tuple(dict.fromkeys((*routing.requests, *published)))
            marks = ",".join("?" for _ in references)
            originals = (
                tuple(
                    source.conversation_sources(
                        lookup,
                        f"w.seq IN ({marks})",
                        tuple(ref.seq for ref in references),
                        limit=len(references),
                        ascending=True,
                    )
                )
                if references
                else ()
            )
            if {item.message.reference for item in originals} != set(references) or len(
                set(published)
            ) != len(published):
                raise RelationViolationError("Publication is not its original seq/id relation")
            routing.require_publications(
                tuple(item for item in originals if item.message.reference in published), lookup
            )
            self._record_publication(
                thread.runtime.read_key, ids, final.entry.id, routing, published
            )

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
