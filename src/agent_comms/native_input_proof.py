"""Native input identity, byte placement and corroborated model-context proof.

These declarations are shared by durable input state, emitted events and the
proof journal without depending on session execution or transcript routing.
"""

from __future__ import annotations

import os
import sqlite3
from collections.abc import Iterator
from contextlib import ExitStack, closing, contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, ClassVar, Literal

from pydantic import JsonValue

from .coordination_errors import IdentityConflict
from .errors import RelationViolationError
from .field_codec import (
    FieldCodec,
    InputId,
    MalformedRecord,
    PathText,
    Sha256Text,
    WireName,
    boundary_decoder,
)
from .locked_store import SQLiteFile
from .native_failures import NativePiUnavailable
from .native_session_reopen import PiSessionId
from .private_path import FileIdentity, PrivateFileRole
from .typed_table import (
    Column,
    DurableTables,
    GuardedInserts,
    ImmutableRows,
    Index,
    IntegerStorage,
    WithoutRowid,
)

if TYPE_CHECKING:
    from . import pi_events as pi
    from .native_entries import NativeEntry, NativeEvidenceRead, SessionEntry
    from .native_session_files import NativeSessionFiles


@dataclass(frozen=True, slots=True)
class NativeInputCommit:
    """Original emitted input identity shared with its assembled context."""

    input_id: Annotated[
        InputId,
        WireName("inputId"),
        Column(primary_key=True),
    ]
    session_id: Annotated[PiSessionId, WireName("sessionId")]
    session_entry_id: Annotated[str, WireName("sessionEntryId")]

    @property
    def input_commit(self) -> NativeInputCommit:
        return NativeInputCommit(self.input_id, self.session_id, self.session_entry_id)


@dataclass(frozen=True)
class NativeInputLocation(NativeInputCommit):
    """Emitted byte placement; only the original runtime input row persists it."""

    # SQL permits an uncommitted row; the emitted offset remains a required int.
    position_column: ClassVar[Column] = Column(
        check="session_entry_start>0",
        nullable=True,
        storage=IntegerStorage,
    )
    session_entry_start: Annotated[
        int, WireName("sessionEntryStart"), position_column
    ] = field(kw_only=True)


@dataclass(frozen=True, slots=True)
class NativeContextRecord(NativeInputCommit):
    """Native context facts extend the original committed input declaration."""

    request_generation: Annotated[
        int,
        WireName("requestGeneration"),
        Column(primary_key=True, check="request_generation BETWEEN 1 AND 9007199254740991"),
    ]
    llm_context_digest: Annotated[
        str,
        Sha256Text,
        WireName("llmContextDigest"),
    ]
    # The native writer stamps the moment this context was committed for a
    # model request: the time the input was shown to the model.
    committed_at_ms: Annotated[
        int,
        WireName("committedAtMs"),
        Column(check="committed_at_ms BETWEEN 1 AND 9007199254740991"),
    ]

    @classmethod
    def from_events(
        cls, input_id: InputId, session_id: PiSessionId,
        input_event: pi.InputCommitted, context_event: pi.ContextCommitted,
    ) -> NativeContextRecord:
        """Strictly join the two emitted records through the context declaration.

        This is an emitted acknowledgement, not proof of durable inclusion. A journal
        read must independently corroborate the same complete located record.
        """
        emitted = FieldCodec.encode(context_event)
        record = FieldCodec.decode(cls, {key: emitted[key]
                                         for _, key in FieldCodec.record_fields(cls)})
        if record.input_commit != NativeInputCommit(input_id, session_id, record.session_entry_id):
            raise NativePiUnavailable("Native Pi context names another original input")
        if record.input_commit != input_event.input_commit:
            raise NativePiUnavailable("Native Pi input/context events disagree")
        return record

    def __post_init__(self):
        Sha256Text.decode(self.llm_context_digest)
        if not 0 < self.request_generation <= 2**53 - 1:
            raise ValueError("Native context generation is outside its native range")
        if not 0 < self.committed_at_ms <= 2**53 - 1:
            raise ValueError("Native context commit time is outside its native range")

    def at(self, session_file: Path, *, session_entry_start: int) -> NativeContextProof:
        """Locate recorded facts; this does not grant acceptance or replay."""
        return NativeContextProof(
            self.input_id,
            self.session_id,
            self.session_entry_id,
            self.request_generation,
            self.llm_context_digest,
            self.committed_at_ms,
            session_file,
            session_entry_start=session_entry_start,
        )


@dataclass(frozen=True, slots=True)
class NativeContextJournal(
    GuardedInserts, ImmutableRows, NativeContextRecord, WithoutRowid, DurableTables
):
    """One proof declaration owns wire facts and the indexed durable journal."""

    insert_condition = """NEW.request_generation < COALESCE((SELECT MAX(request_generation)
        FROM native_context_journal), 0) OR EXISTS(SELECT 1 FROM native_context_journal
        WHERE request_generation=NEW.request_generation
        AND (llm_context_digest != NEW.llm_context_digest
        OR committed_at_ms != NEW.committed_at_ms)) OR NEW.session_id
        != (SELECT session_id FROM native_context_journal ORDER BY session_id LIMIT 1)"""


    schema: Literal[2]
    type: Literal["context_committed"]

    indexes = (Index(("request_generation",)), Index(("session_id",)))

    @classmethod
    def native_contract(cls) -> dict[str, JsonValue]:
        """Generate the native writer's SQLite contract from this declaration."""
        table = cls.declared_name
        return {
            "objects": cls.schema_objects(),
            "columns": [
                {"name": plan.name, "wire": plan.key} for plan in FieldCodec.field_plans(cls)
            ],
            "insert": cls.insertion_statement(),
            "head": f"SELECT COALESCE(MAX(request_generation),0) AS generation FROM {table}",
            "session": f"SELECT session_id AS id FROM {table} ORDER BY session_id LIMIT 1",
            "current": f"SELECT input_id,session_entry_id FROM {table} "
            f"WHERE request_generation=(SELECT MAX(request_generation) FROM {table})",
        }

    @classmethod
    @contextmanager
    def open_evidence(cls, files: NativeSessionFiles) -> Iterator[sqlite3.Connection]:
        """Read an indexed snapshot after SQLite's bounded hot-journal recovery.

        A read is corroboration, never a fresh acknowledgement. Keep the private inode
        pinned across SQLite's transaction and refuse redirection or schema drift.
        Open existing storage read/write for SQLite rollback, then forbid SQL
        writes. A cold history view can recover without a model turn or acknowledgement.
        """
        path = files.input_proof
        with ExitStack() as ownership:
            try:
                held = ownership.enter_context(closing(
                    os.fdopen(PrivateFileRole.read(
                        path, NativePiUnavailable("Native proof indexed evidence is unavailable")), "rb")
                ))
                before = os.fstat(held.fileno())
                db = ownership.enter_context(closing(SQLiteFile.recovering_snapshot(path)))
                if cls.schema_differs(db):
                    raise NativePiUnavailable("Native proof requires offline durable conversion")
                if FileIdentity.from_stat(before) != FileIdentity.from_stat(path.lstat()):
                    raise NativePiUnavailable("Native proof inode changed while opening")
            except (OSError, sqlite3.Error, RelationViolationError, MalformedRecord) as error:
                raise NativePiUnavailable("Native proof indexed evidence is unavailable") from error
            # Consumer exceptions retain their original boundary and disposition.
            # The ExitStack closes the database and descriptor without handling caller errors.
            yield db
            try:
                if FileIdentity.from_stat(before) != FileIdentity.from_stat(path.lstat()):
                    raise NativePiUnavailable("Native proof inode changed during observation")
            except OSError as error:
                raise NativePiUnavailable("Native proof indexed evidence is unavailable") from error

    @classmethod
    def for_input(
        cls, db: sqlite3.Connection, input_id: InputId, generation: int | None = None
    ) -> NativeContextJournal | None:
        """The primary key answers one input without scanning lifetime history."""
        where = "input_id=?"
        parameters: tuple[InputId | int, ...] = (input_id,)
        if generation is not None:
            where += " AND request_generation=?"
            parameters += (generation,)
        rows = cls.read(
            db.execute(
                f"SELECT {cls._column_list(tuple(cls.column_projection()))} FROM {cls.declared_name} "
                f"WHERE {where} ORDER BY request_generation DESC LIMIT 1",
                parameters,
            )
        )
        return next(iter(rows), None)

    def corroborate(
        self, session_file: Path, header: SessionEntry, entry: NativeEntry, *,
        session_entry_start: int,
    ) -> NativeContextProof:
        if self.session_id != header.id or self.session_entry_id != entry.id:
            raise NativePiUnavailable("Native Pi proof journal source lineage differs")
        return self.at(session_file, session_entry_start=session_entry_start)


@dataclass(frozen=True, slots=True)
class NativeContextProof(NativeContextRecord, NativeInputLocation):
    session_file: Annotated[Path, PathText]

    def require_continuation(self, context: NativeContextProof) -> None:
        """Repeated requests may include this input; its first receipt stays immutable.

        A different native entry or conflicting proof of the same request is not
        another observation of that entry. Callers corroborate later generations
        before this check; this comparison grants no native or replay authority.
        """
        if (self.input_commit, self.session_file, self.session_entry_start) != (
            context.input_commit, context.session_file, context.session_entry_start
        ):
            raise IdentityConflict("Native context acknowledgement belongs to another entry")
        if context.request_generation < self.request_generation:
            raise IdentityConflict("Native context precedes its original acknowledgement")
        if context.request_generation == self.request_generation and context != self:
            raise IdentityConflict("Native request has contradictory context evidence")

    def corroborates_input(
        self, input_id: InputId, *, files: NativeSessionFiles,
        evidence: NativeEvidenceRead,
    ) -> bool:
        """Check an already-observed live event against its isolated saved proof.

        This grants neither replay nor recovery authority. History IO occurs
        before the coordinator read transaction, as on the native return path.
        """
        if self.input_id != input_id or files.session != self.session_file:
            return False
        return self.read_evidence(
            files, input_id,
            request_generation=self.request_generation, evidence=evidence,
            session_entry_start=self.session_entry_start,
        ) == self

    @classmethod
    @boundary_decoder
    def read_evidence(
        cls, files: NativeSessionFiles, input_id: InputId, *, request_generation: int | None = None,
        evidence: NativeEvidenceRead | None = None,
        session_entry_start: int,
    ) -> NativeContextProof:
        """Corroborate live recorded events; parsed bytes alone grant no authority."""
        session_file = files.session.absolute()
        from .native_entries import NativeInputEvidenceRead
        with NativeInputEvidenceRead.borrow(session_file, evidence) as evidence:
            with NativeContextJournal.open_evidence(files) as db:
                row = NativeContextJournal.for_input(db, input_id, request_generation)
            if row is None:
                raise NativePiUnavailable("The input has no assembled-context proof")
            location = NativeInputLocation(input_id, row.session_id, row.session_entry_id,
                                           session_entry_start=session_entry_start)
            entry = evidence.read_input(location)
            return row.corroborate(session_file, evidence.header(), entry,
                                   session_entry_start=session_entry_start)
