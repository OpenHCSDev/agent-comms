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
from typing import Annotated, ClassVar, Literal

from pydantic import JsonValue

from .coordination_errors import IdentityConflict
from .errors import RelationViolationError
from .nominal.field_codec import (
    FieldCodec,
    InputId,
    MalformedRecord,
    PathText,
    Sha256Text,
    WireName,
)
from .locked_store import DurableFile, PatternFile, SQLiteFile
from .native_failures import NativePiUnavailable
from .native_integer import PositiveNativeIntegerBounds
from .native_session_reopen import PiSessionId
from .private_path import FileIdentity, PrivateFileRole
from .turn_item import FreshNativeId
from .typed_table import (
    Column,
    DurableTables,
    GuardedInserts,
    ImmutableRows,
    Index,
    IntegerStorage,
    ReconciledFile,
    TableSchema,
    TypedTable,
    WithoutRowid,
)


@dataclass(frozen=True, slots=True)
class NativeInputCommit:
    """Original emitted input identity shared with its assembled context."""

    input_id: Annotated[
        InputId,
        WireName("inputId"),
        Column(primary_key=True),
    ]
    session_id: Annotated[PiSessionId, WireName("sessionId")]
    session_entry_id: Annotated[FreshNativeId, WireName("sessionEntryId")]

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
    session_entry_start: Annotated[int, WireName("sessionEntryStart"), position_column] = field(
        kw_only=True
    )


@dataclass(frozen=True, slots=True)
class NativeContextRecord(NativeInputCommit, PositiveNativeIntegerBounds):
    """Native context facts extend the original committed input declaration."""

    request_generation: Annotated[
        int,
        WireName("requestGeneration"),
        Column(primary_key=True, check=PositiveNativeIntegerBounds.sql_check("request_generation")),
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
        Column(check=PositiveNativeIntegerBounds.sql_check("committed_at_ms")),
    ]

    def __post_init__(self) -> None:
        Sha256Text.decode(self.llm_context_digest)
        self.require_integer(self.request_generation, "Native context generation")
        self.require_integer(self.committed_at_ms, "Native context commit time")

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
            "objects": {name: statement for name, statement in cls.schema_objects().items()},
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
    def open_evidence(cls, path: Path) -> Iterator[sqlite3.Connection]:
        """Read an indexed snapshot after SQLite's bounded hot-journal recovery.

        A read is corroboration, never a fresh acknowledgement. Keep the private inode
        pinned across SQLite's transaction and refuse redirection or schema drift.
        Open existing storage read/write for SQLite rollback, then forbid SQL
        writes. A cold history view can recover without a model turn or acknowledgement.
        """
        with ExitStack() as ownership:
            try:
                held = ownership.enter_context(
                    closing(
                        os.fdopen(
                            PrivateFileRole.read(
                                path,
                                NativePiUnavailable("Native proof indexed evidence is unavailable"),
                            ),
                            "rb",
                        )
                    )
                )
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
        cls,
        db: sqlite3.Connection,
        input_id: InputId,
        generations: range = NativeContextRecord.interval(),
    ) -> NativeContextJournal:
        """Require the latest input proof in this indexed generation interval."""
        if generations.step != 1:
            raise ValueError("Context proof generations require a contiguous interval")
        rows = cls.read(
            db.execute(
                f"SELECT {cls._column_list(tuple(cls.column_projection()))} "
                f"FROM {cls.declared_name} "
                "WHERE input_id=? AND request_generation>=? AND request_generation<? "
                "ORDER BY request_generation DESC LIMIT 1",
                (input_id, generations.start, generations.stop),
            )
        )
        try:
            return next(iter(rows))
        except StopIteration as missing:
            raise NativePiUnavailable("The input has no assembled-context proof") from missing

    def corroborate(
        self,
        session_file: Path,
        location: NativeInputLocation,
    ) -> NativeContextProof:
        if self.input_commit != location.input_commit:
            raise NativePiUnavailable("Native Pi proof journal source lineage differs")
        return self.at(session_file, session_entry_start=location.session_entry_start)


class NativeInputProof(DurableFile, ReconciledFile, PatternFile, DurableTables):
    """The retained proof file names its journal without joining their two families."""

    __slots__ = ()
    filename = "input-proofs/{lookup}/{name}.sqlite"

    @classmethod
    def table_schema(cls) -> type[TableSchema]:
        return cls

    @classmethod
    def schema_rows(cls) -> tuple[type[TypedTable], ...]:
        return (NativeContextJournal,)


@dataclass(frozen=True, slots=True)
class NativeContextProof(NativeContextRecord, NativeInputLocation):
    session_file: Annotated[Path, PathText]

    def require_continuation(self, context: NativeContextProof) -> None:
        """Repeated requests may include this input; its first proof stays immutable.

        A different native entry or conflicting proof of the same request is not
        another observation of that entry. Callers corroborate later generations
        before this check; this comparison grants no native or replay authority.
        """
        if (self.input_commit, self.session_file, self.session_entry_start) != (
            context.input_commit,
            context.session_file,
            context.session_entry_start,
        ):
            raise IdentityConflict("Native context acknowledgement belongs to another entry")
        if context.request_generation < self.request_generation:
            raise IdentityConflict("Native context precedes its original acknowledgement")
        if context.request_generation == self.request_generation and context != self:
            raise IdentityConflict("Native request has contradictory context evidence")
