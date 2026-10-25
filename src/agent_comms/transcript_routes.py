"""Routing annotations keyed by durable Pi entry IDs, never reply text."""

from __future__ import annotations

import os
import re
from contextlib import ExitStack, closing
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Annotated, ClassVar, TYPE_CHECKING

from agent_comms.native_session_files import NativeSessionFiles
from agent_comms.native_session_reopen import NativeSessionIdentity

from .errors import RelationViolationError
from .field_codec import InputId, MalformedRecord, NominalSpelling, NominalText, NullMember, SingleFieldValue
from .input_disposition import InputDispositions
from .private_path import FileObservation, FileRevision
from .routing import NoTurnRouting, Routing, TurnRouting
from .text_digest import TextDigest
from .transcript_events import ContextTranscript, TranscriptEvent
from .typed_table import Column, DurableStore, StoreRows, TextStorage, WithoutRowid


if TYPE_CHECKING:
    from sqlite3 import Connection

    from .native_entries import TranscriptProjection
    from .pi_payloads import UserMessage


class TranscriptRoutesRows(StoreRows):
    """The tables of TranscriptRoutes; each row declares itself one."""


@dataclass(frozen=True)
class TranscriptRoute(TranscriptRoutesRows, WithoutRowid):
    session_file: Annotated[str, Column(primary_key=True)]
    entry_id: Annotated[str, Column(primary_key=True)]
    routing: TurnRouting

    def require_publication(self, routing: TurnRouting) -> None:
        if self.routing.publications and self.routing != routing:
            raise RelationViolationError("Original native publication cannot be rebound")


class InputText(NominalText):
    """Exact input text, including empty strings and embedded newlines."""


class InputTextSpelling(NominalSpelling[InputText]):
    spelling = re.compile(r"[\s\S]*")


class DisplayText(InputText):
    def prompt_or(self, fallback: str) -> str:
        return self.spelled_by.encode(self) or fallback


@dataclass(frozen=True)
class NoDisplayText(NullMember):
    """The input is internal and contributes no human display text."""

    def prompt_or(self, fallback: str) -> str:
        return fallback


class SentTextDigest(TextDigest, SingleFieldValue):
    """The exact sent text owns matching and an immutable route binding."""

    def project_message(
        self, message: UserMessage, display: InputDisplay,
        context: TranscriptProjection, timestamp: float,
    ) -> list[TranscriptEvent]:
        if not message.matches_display(self):
            return message.project_parts(message.parts, context, timestamp, NoTurnRouting())
        return display.project_display(message, context, timestamp, display.routing)

    def record(self, previous: InputDisplay, connection: Connection, routing: Routing) -> None:
        previous.sent_text_digest.bind(self, previous, connection, routing)

    def bind(
        self, incoming: SentTextDigest, previous: InputDisplay,
        connection: Connection, routing: Routing,
    ) -> None:
        if (self, previous.routing) != (incoming, routing):
            raise RelationViolationError("Native input routing cannot be rebound.")


@dataclass(frozen=True)
class NoSentTextDigest(NullMember):
    """No exact sent text was bound; retain the surrounding route."""

    def project_message(
        self, message: UserMessage, display: InputDisplay,
        context: TranscriptProjection, timestamp: float,
    ) -> list[TranscriptEvent]:
        return display.project_display(message, context, timestamp, context.routing)

    def record(self, previous: InputDisplay, connection: Connection, routing: Routing) -> None:
        pass

    def bind(
        self, incoming: SentTextDigest, previous: InputDisplay,
        connection: Connection, routing: Routing,
    ) -> None:
        InputDisplay.update(
            connection, where="native_id=?", parameters=(previous.native_id,),
            routing=routing, sent_text_digest=incoming,
        )


class SentInputText(InputText):
    @property
    def digest(self) -> SentTextDigest:
        return SentTextDigest.of(self)


@dataclass(frozen=True)
class NoSentInputText(NullMember):
    @property
    def digest(self) -> NoSentTextDigest:
        return NoSentTextDigest()


class InputPresentation:
    """A native input's display policy; the default preserves its original parts."""

    def project_message(
        self, message: UserMessage, context: TranscriptProjection, timestamp: float,
    ) -> list[TranscriptEvent]:
        return message.project_parts(message.parts, context, timestamp, context.routing)


@dataclass(frozen=True)
class NoInputDisplay(NullMember, InputPresentation):
    """No owner-authored replacement or suppression was recorded."""


@dataclass(frozen=True)
class InputDisplay(TranscriptRoutesRows, WithoutRowid, InputPresentation):
    """Owner-authored presentation for one native input."""

    native_id: Annotated[InputId, Column(primary_key=True)]
    text: DisplayText | NoDisplayText
    routing: Routing = NoTurnRouting()
    sent_text_digest: Annotated[
        SentTextDigest | NoSentTextDigest, Column(storage=TextStorage),
    ] = NoSentTextDigest()
    checks = ("routing IS NULL OR sent_text_digest IS NOT NULL",)

    def project_message(
        self, message: UserMessage, context: TranscriptProjection, timestamp: float,
    ) -> list[TranscriptEvent]:
        return self.sent_text_digest.project_message(message, self, context, timestamp)

    def project_display(
        self, message: UserMessage, context: TranscriptProjection,
        timestamp: float, routing: Routing,
    ) -> list[TranscriptEvent]:
        raw_text = message.original_text
        events: list[TranscriptEvent] = []
        if raw_text and raw_text != self.text:
            events.append(ContextTranscript(raw_text, position=context.position, timestamp=timestamp))
        for text in self.text.present():
            events.extend(message.project_parts(
                message.display_parts(text.spelled_by.encode(text)), context, timestamp, routing,
            ))
        return events


class _SessionRoutes:
    """One indexed read snapshot; decode only entries used by the page."""

    def __init__(self, store: TranscriptRoutes, session_file: str):
        self._lifetime = ExitStack()
        self._connection = self._lifetime.enter_context(store.snapshot())
        self._session_file = session_file
        self._cache: dict[str, Routing] = {}
        self._input_cache: dict[str, InputPresentation] = {}

    def get(self, entry_id: str | None) -> Routing:
        if entry_id is None:
            return NoTurnRouting()
        if entry_id not in self._cache:
            self._cache[entry_id] = next(
                (row.routing for row in TranscriptRoute.select(
                    self._connection, session_file=self._session_file, entry_id=entry_id
                )), NoTurnRouting(),
            )
        return self._cache[entry_id]

    def input_display(self, native_id: InputId | None) -> InputPresentation:
        if native_id is None:
            return NoInputDisplay()
        if native_id not in self._input_cache:
            self._input_cache[native_id] = next(
                iter(InputDisplay.select(self._connection, native_id=native_id)), NoInputDisplay(),
            )
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
    tables = TranscriptRoutesRows

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
        with log.reading() as source, registry.store.reading_snapshot() as snapshot:
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
            originals = (
                source.conversation(
                    lookup,
                    sequences=tuple(ref.seq for ref in references),
                    limit=len(references),
                    descending=False,
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
        native_id: InputId,
        display_text: DisplayText | NoDisplayText,
        *,
        sent_text: SentInputText | NoSentInputText = NoSentInputText(),
        routing: Routing = NoTurnRouting(),
    ) -> None:
        """Persist before prompt write, including before Pi creates its session file."""
        routing.validate_input(sent_text)
        digest = sent_text.digest
        with self.writing() as connection:
            previous = InputDisplay.one(connection, native_id=native_id)
            if previous is None:
                InputDisplay(native_id, display_text, routing, digest).insert(connection)
            else:
                digest.record(previous, connection, routing)
