"""Durable, non-replayable compaction intents (not an authority mechanism).

The owner commit bridge must call this inside its registry authority scope.
Native reconciliation evidence must be collected under the native writer lock
before calling ``resolve``. No method here dispatches or retries native work.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import stat
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import TYPE_CHECKING, Literal
from uuid import uuid4
from weakref import WeakKeyDictionary

from .child_process import ProcessIdentity
from .field_codec import FieldCodec
from .input_attempt import InputAttempt
from .input_disposition import InputDispositions, InputDocument
from .owner_compaction_settings import PiCompactionSettings
from .pi_summary_payloads import SelectedModel
from .selected_source import SelectedSource
from .text_digest import TextDigest
from .thread_identity import ThreadIncarnation
from .typed_table import Column, Index, TypedRow, TypedTable

if TYPE_CHECKING:
    from .fresh_private_session import FreshPrivateSession
    from .selected_summary_admission import SelectedAdmissionIdentity, SelectedSummaryAdmission

from .compaction_states import (
    CommittedNativeOutcome,
    DeclinedPrestartSummary,
    IntentOperation,
    LinkedSummary,
    ObservedPublication,
    OperationState,
    PendingPublication,
    PublicationState,
    ReservedSummary,
    SummaryState,
    UnknownSummary,
    sql_names,
)


class _ReturnedTerminalAck:
    """Only the successful post-COMMIT parent fsync creates this identity."""

    __slots__ = ("__weakref__",)


_issued_selected_acks: WeakKeyDictionary[_ReturnedTerminalAck, tuple] = WeakKeyDictionary()
# A visible enrollment SQL row after lost parent fsync is NOT coverage authority.
# Only its exact returned registration can allow a same-process private attempt.
_returned_fresh_enrollments: WeakKeyDictionary[FreshPrivateSession, tuple] = WeakKeyDictionary()


def _consume_selected_ack(receipt: object, expected: tuple) -> bool:
    """Consume exact one-use post-fsync receipt, never derive one from a row."""
    if type(receipt) is not _ReturnedTerminalAck:
        return False
    recorded = _issued_selected_acks.pop(receipt, None)
    return recorded == expected


class CompactionJournalError(RuntimeError):
    """Journal state cannot authorize another dispatch; fail closed."""


class CompactionJournalUnknownError(CompactionJournalError):
    """A COMMIT may be visible but lacks durable confirmation; never dispatch/replay."""


@dataclass(frozen=True)
class SelectedSummarySource:
    """Journaled request evidence, not a returned input-admission capability.

    The admission owner validates its original/turn witness; the journal retains
    its exact source data and owns only durable exclusion and native linkage.
    """

    source: SelectedSource
    selected: SelectedModel
    settings: PiCompactionSettings

    def __post_init__(self):
        if not self.source:
            raise ValueError("Selected source witness required")


class JournalTable:
    """Tables whose schema and transactions belong to the compaction journal."""


@dataclass(frozen=True)
class CompactionOperation(JournalTable, TypedTable, declared_name="operations"):
    commit_id: str = field(metadata={"sql": Column(primary_key=True)})
    session_file: str
    intent_json: str
    state: OperationState
    evidence_json: str | None

    indexes = (
        Index(
            ("session_file",),
            unique=True,
            where=f"json_extract(state, '$.kind') IN {sql_names(OperationState, unresolved=True)}",
        ),
    )


@dataclass(frozen=True)
class SelectedSummaryAttempt(JournalTable, TypedTable, declared_name="selected_summary_attempts"):
    """Provider attempt reservation, not a summary or native commit receipt."""

    operation_id: str = field(metadata={"sql": Column(primary_key=True)})
    session_file: str
    source_json: str
    state: SummaryState

    indexes = (
        Index(
            ("session_file",),
            unique=True,
            where=f"json_extract(state, '$.kind') IN {sql_names(SummaryState, unresolved=True)}",
        ),
    )

    def original_has_started(self, inputs: dict[str, InputAttempt]) -> bool:
        """Completed input evidence retires this barrier, never recreates a send token.

        The existing input ledger owns native-start proof. A linked/declined
        summary alone, a bound UNKNOWN input, or an unrelated started input
        cannot retire the reservation. Historical rows and IDs stay intact.
        """
        if not self.state.original_eligible:
            return False
        try:
            envelope = FieldCodec.decode(SelectedSummarySource, json.loads(self.source_json))
            return envelope.source.original_has_started(InputDocument(rows=inputs))
        except (KeyError, TypeError, ValueError):
            return False


@dataclass(frozen=True)
class CompactionPublication(JournalTable, TypedTable, declared_name="publications"):
    """Local metadata-only projection, keyed by native commit ID; no recipient."""

    commit_id: str = field(
        metadata={"sql": Column(primary_key=True, references=(CompactionOperation, "commit_id"))}
    )
    session_file: str
    metadata_json: str
    state: PublicationState


@dataclass(frozen=True)
class PrivateRawInput(JournalTable, TypedTable, declared_name="private_raw_inputs"):
    input_id: str = field(metadata={"sql": Column(primary_key=True)})
    session_file: str
    status: Literal["unknown"] = "unknown"


@dataclass(frozen=True)
class EnrolledPrivateSession(JournalTable, TypedTable, declared_name="enrolled_private_sessions"):
    session_file: str = field(metadata={"sql": Column(primary_key=True)})
    session_id: str = field(metadata={"sql": Column(unique=True)})
    device: int
    inode: int
    header_sha256: str
    incarnation: ThreadIncarnation
    owner_lookup: str
    owner_generation: int
    admission_generation: int
    creator: ProcessIdentity


@dataclass(frozen=True)
class JournalSchemaObject(TypedRow):
    name: str
    sql: str


@dataclass(frozen=True)
class JournalMode(TypedRow):
    journal_mode: str


@dataclass(frozen=True)
class SyncMode(TypedRow):
    synchronous: int


def _publication_metadata(commit_id: str, evidence: dict) -> str:
    try:
        committed = FieldCodec.decode(CommittedNativeOutcome, evidence)
    except (ValueError, TypeError) as error:
        raise CompactionJournalError("Exact committed native metadata required") from error
    return json.dumps(
        FieldCodec.encode(committed.publication(commit_id)),
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


class CompactionJournal:
    """One durable journal per wire; at most one unresolved op per session.

    Parent directory must already exist (the registry owns its creation).
    Verified SQLite EXTRA plus post-COMMIT directory fsync make successful
    ``begin`` durable before dispatch. If begin raises, DO NOT dispatch; if
    outcome persistence raises, the durable intent remains unresolved.
    """

    def __init__(self, path: Path):
        self.path = path
        if os.name != "posix":
            raise NotImplementedError("Compaction journal bridge requires POSIX")
        # Claim a private file before SQLite opens it; never truncate existing
        # history. Registry serialization covers normal callers; SQLite also
        # protects uniqueness against independent connections.
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            if path.is_symlink() or not path.is_file():
                raise CompactionJournalError("Journal must be a regular private file") from None
        else:
            os.close(fd)
        with self._transaction() as db:
            tables = TypedTable.members_with(JournalTable)
            expected = {sql for table in tables for sql in table.ddl()}
            actual = {
                row.sql
                for row in JournalSchemaObject.read(
                    db.execute("SELECT name,sql FROM sqlite_master WHERE sql IS NOT NULL")
                )
            }
            if not actual:
                for table in tables:
                    table.create(db)
            elif actual != expected:
                raise CompactionJournalError(
                    "Compaction journal schema differs; quiet migration required"
                )
        parent = path.parent.resolve(strict=True)
        for directory in (parent, *parent.parents):
            parent_fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(parent_fd)
            finally:
                os.close(parent_fd)

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        """Durable generic transaction; never mints a selected admission ACK.

        Only the verified terminal methods below issue their one-use receipt,
        after this exact transaction's commit and parent fsync have returned.
        A caller's status-only SQL transition cannot use this generic API to
        construct input authority.
        """
        db = sqlite3.connect(self.path, timeout=5)
        try:
            mode = JournalMode.read(db.execute("PRAGMA journal_mode=DELETE"))
            db.execute("PRAGMA synchronous=EXTRA")
            if mode != [JournalMode("delete")] or SyncMode.read(
                db.execute("PRAGMA synchronous")
            ) != [SyncMode(3)]:
                raise CompactionJournalError("Required durable SQLite mode unavailable")
            db.execute("BEGIN IMMEDIATE")
            try:
                yield db
            except BaseException:
                db.rollback()
                raise
            try:
                db.commit()
                # EXTRA syncs the rollback-journal unlink. Explicitly sync the
                # parent too, before returning ANY committed intent/outcome.
                # Constructor-only directory sync cannot cover this unlink.
                directory_fd = os.open(self.path.parent, os.O_RDONLY | os.O_DIRECTORY)
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
            except Exception as error:
                raise CompactionJournalUnknownError(
                    "Compaction journal durability UNKNOWN; never dispatch or replay"
                ) from error
        finally:
            db.close()

    def begin(
        self,
        session_file: str,
        intent: dict,
        *,
        inputs: InputDocument,
        commit_id: str | None = None,
    ) -> str:
        """Durably reserve under the caller's retained input snapshot lock.

        OwnerCompactionCommit already holds the exclusive disposition lock;
        consuming that snapshot avoids reacquisition and preserves input-before-
        journal ordering. Every reused ID is forbidden forever.
        """
        commit_id = uuid4().hex if commit_id is None else commit_id
        if not re.fullmatch(r"[0-9a-f]{32}", commit_id):
            raise ValueError("Expected exact compaction commit ID")
        canonical = str(Path(session_file).resolve(strict=True))
        payload = json.dumps(intent, sort_keys=True, separators=(",", ":"), allow_nan=False)
        if len(payload.encode()) > 65536:
            raise ValueError("Compaction intent exceeds bound")
        try:
            with self._transaction() as db:
                selected = self._blocking_selected_summary(db, canonical, inputs)
                if selected and (
                    len(selected) != 1
                    or type(intent) is not dict
                    or intent.get("selectedSummaryOperationId") != selected[0].operation_id
                ):
                    raise CompactionJournalError(
                        "Blocked selected summary; unrelated native commit forbidden"
                    )
                if selected:
                    selected[0].state.require_commit_reservation()
                CompactionOperation(commit_id, canonical, payload, IntentOperation(), None).insert(
                    db
                )
        except sqlite3.IntegrityError as error:
            raise CompactionJournalError(
                "Unresolved session or reused commit ID; never replay"
            ) from error
        return commit_id

    def get(self, commit_id: str) -> CompactionOperation:
        with self._transaction() as db:
            row = CompactionOperation.one(db, commit_id=commit_id)
        if row is None:
            raise CompactionJournalError("Unknown compaction operation")
        return row

    def unresolved(self, session_file: str) -> tuple[CompactionOperation, ...]:
        """Discover crash-orphaned intents for explicit recovery, never dispatch."""
        canonical = str(Path(session_file).resolve(strict=True))
        with self._transaction() as db:
            rows = CompactionOperation.select(
                db,
                where=(
                    "session_file=? AND json_extract(state, '$.kind') IN "
                    f"{sql_names(OperationState, unresolved=True)}"
                ),
                parameters=(canonical,),
            )
        return tuple(rows)

    def enroll_fresh_private_session(
        self,
        fresh: FreshPrivateSession,
        *,
        incarnation: ThreadIncarnation,
        owner_lookup: str,
        owner_generation: int,
        admission_generation: int,
    ) -> None:
        """Persist exact O_EXCL new-session coverage under the caller's owner locks.

        A file/header alone cannot be enrolled later: ``fresh`` is a returned
        process-local creation object, and this transaction's COMMIT + parent
        fsync must return before any native prompt. The caller must first
        verify its current registry owner/SQL generation under the shared wire
        lock. This row is NOT terminal raw-input settlement or selected grant.
        """
        from .fresh_private_session import FreshPrivateSession

        if type(fresh) is not FreshPrivateSession:
            raise CompactionJournalError("Returned fresh-session creation required")
        fresh.verify_prewrite()
        if type(owner_lookup) is not str or not owner_lookup or "/" in owner_lookup:
            raise CompactionJournalError("Exact private owner lookup required")
        private_root = (self.path.parent / "native-sessions").resolve(strict=False)
        if (
            fresh.path.resolve(strict=True).parent != private_root / owner_lookup
            or type(owner_generation) is not int
            or owner_generation <= 0
            or type(admission_generation) is not int
            or admission_generation <= 0
        ):
            raise CompactionJournalError("Fresh-session owner or private location differs")
        try:
            with self._transaction() as db:
                if any(
                    table.select(db, where="session_file=? LIMIT 1", parameters=(str(fresh.path),))
                    for table in (PrivateRawInput, SelectedSummaryAttempt, CompactionOperation)
                ):
                    raise CompactionJournalError("Fresh-session history already exists")
                fresh.verify_prewrite()
                enrollment = EnrolledPrivateSession(
                    str(fresh.path),
                    fresh.session_id,
                    fresh.device,
                    fresh.inode,
                    fresh.header_sha256,
                    incarnation,
                    owner_lookup,
                    owner_generation,
                    admission_generation,
                    ProcessIdentity.capture(fresh.creator_pid),
                )
                enrollment.insert(db)
        except sqlite3.IntegrityError as error:
            raise CompactionJournalError("Fresh-session coverage already enrolled") from error
        _returned_fresh_enrollments[fresh] = (str(self.path), enrollment)

    def reserve_selected_summary(
        self,
        session_file: str,
        source: dict,
        *,
        operation_id: str | None = None,
        fresh_session: FreshPrivateSession | None = None,
        admission_generation: int | None = None,
    ) -> str:
        """Durably reserve BEFORE any selected Pi RPC send or auth side effect.

        All selected attempts, including terminal-looking ones, remain blocking
        until separately reviewed exact-ID recovery exists. A failure before
        writing stdin does not authorize retry.
        The caller must separately retain owner/turn/ingress authority; this
        journal is only an exclusion and recovery record, never a bearer grant.
        """
        operation_id = uuid4().hex if operation_id is None else operation_id
        if not re.fullmatch(r"[0-9a-f]{32}", operation_id):
            raise ValueError("Expected exact selected summary operation ID")
        canonical = str(Path(session_file).resolve(strict=True))
        info = Path(canonical).lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            # An outward hardlink can alias a private saved inode while its
            # lexical path evades the private-root floor. Never reserve on a
            # multiply linked inode, regardless of the supplied path.
            raise CompactionJournalError("Selected saved session must have one private inode link")
        private_sessions = (self.path.parent / "native-sessions").resolve(strict=False)
        private = Path(canonical).is_relative_to(private_sessions)
        if private and fresh_session is not None:
            # Never infer coverage from a visible file, missing marker, or an
            # enrolment SQL row alone after an uncertain fsync/restart. The
            # original O_EXCL creation object must still be in this process.
            from .fresh_private_session import FreshPrivateSession

            if type(fresh_session) is not FreshPrivateSession:
                raise CompactionJournalError(
                    "Private selected reservation requires reviewed raw-history coverage floor"
                )
            fresh_session.verify_saved_identity()
            if fresh_session.path != Path(canonical) or (
                admission_generation is not None and type(admission_generation) is not int
            ):
                raise CompactionJournalError("Fresh private selected identity changed")
        envelope = FieldCodec.decode(SelectedSummarySource, source)
        payload = json.dumps(
            FieldCodec.encode(envelope), sort_keys=True, separators=(",", ":"), allow_nan=False
        )
        if len(payload.encode()) > 65536:
            raise ValueError("Selected summary source exceeds bound")
        from .backend import _session_revision

        envelope.source.reservation_check(
            _session_revision(canonical),
            InputDispositions(self.path.parent / InputDispositions.filename).read(),
        ).require_valid()
        try:
            with (
                InputDispositions(
                    self.path.parent / InputDispositions.filename
                ).reading() as inputs,
                self._transaction() as db,
            ):
                if private and fresh_session is not None:
                    assert fresh_session is not None
                    coverage = EnrolledPrivateSession.one(db, session_file=canonical)
                    witness = envelope.source
                    if (
                        coverage is None
                        or _returned_fresh_enrollments.get(fresh_session)
                        != (str(self.path), coverage)
                        or witness.incarnation != coverage.incarnation
                        or Path(canonical).parent.name != coverage.owner_lookup
                        or witness.owner != coverage.creator
                        or (
                            admission_generation is not None
                            and admission_generation != coverage.admission_generation
                        )
                    ):
                        raise CompactionJournalError("Fresh private owner coverage differs")
                    fresh_session.verify_saved_identity()
                raw_ids = frozenset(
                    row.input_id
                    for row in PrivateRawInput.select(
                        db, where="session_file=?", parameters=(canonical,)
                    )
                )
                if private and fresh_session is None:
                    from .continued_private_session import verify_continued_private_session

                    try:
                        verify_continued_private_session(
                            self.path.parent, Path(canonical), envelope.source, raw_ids
                        )
                    except (OSError, ValueError, sqlite3.Error, RuntimeError) as error:
                        raise CompactionJournalError(
                            "Private selected reservation requires reviewed "
                            "raw-history coverage floor"
                        ) from error
                if CompactionOperation.select(
                    db,
                    where=(
                        "session_file=? AND json_extract(state, '$.kind') IN "
                        f"{sql_names(OperationState, unresolved=True)} LIMIT 1"
                    ),
                    parameters=(canonical,),
                ):
                    raise CompactionJournalError("Unresolved native commit; no selected summary")
                if self._blocking_selected_summary(db, canonical, inputs) or (
                    raw_ids and (not private or fresh_session is not None)
                ):
                    raise CompactionJournalError("Blocked selected summary; never replay")
                SelectedSummaryAttempt(operation_id, canonical, payload, ReservedSummary()).insert(
                    db
                )
        except sqlite3.IntegrityError as error:
            raise CompactionJournalError(
                "Blocked selected summary or reused operation ID; never replay"
            ) from error
        return operation_id

    def selected_summary(self, operation_id: str) -> SelectedSummaryAttempt:
        with self._transaction() as db:
            row = SelectedSummaryAttempt.one(db, operation_id=operation_id)
        if row is None:
            raise CompactionJournalError("Unknown selected summary operation")
        return row

    def unresolved_selected_summary(self, session_file: str) -> tuple[SelectedSummaryAttempt, ...]:
        """Crash-orphaned reservations block every subsequent input send."""
        canonical = str(Path(session_file).resolve(strict=True))
        with self._transaction() as db:
            rows = SelectedSummaryAttempt.select(
                db,
                where=(
                    "session_file=? AND json_extract(state, '$.kind') IN "
                    f"{sql_names(SummaryState, unresolved=True)}"
                ),
                parameters=(canonical,),
            )
        return tuple(rows)

    def _blocking_selected_summary(
        self, db: sqlite3.Connection, canonical: str, inputs: InputDocument
    ) -> tuple[SelectedSummaryAttempt, ...]:
        return tuple(
            attempt
            for attempt in SelectedSummaryAttempt.select(
                db, where="session_file=?", parameters=(canonical,)
            )
            if not attempt.state.settled_without_original
            and not attempt.original_has_started(inputs.rows)
        )

    def blocking_selected_summary(self, session_file: str) -> tuple[SelectedSummaryAttempt, ...]:
        """Block until the exact original input has durable native-start evidence.

        Terminal-looking summary rows never grant a send or mint a token. Once
        the original has actually started, the existing disposition prevents
        replay while ordinary new inputs and later compactions can proceed.
        """
        canonical = str(Path(session_file).resolve(strict=True))
        with (
            InputDispositions(self.path.parent / InputDispositions.filename).reading() as inputs,
            self._transaction() as db,
        ):
            return self._blocking_selected_summary(db, canonical, inputs)

    def reserve_private_raw_input(self, session_file: Path, input_id: str) -> None:
        """Durably mark exact private-session raw input UNKNOWN before any pipe write.

        Called after PR94's owner/claim/prompt binding checks under its shared
        wire lock. The marker lives in the SAME journal as selected reservation,
        so their write transactions serialize. This returned fsync ACK permits
        only attempting the one raw write; it never proves provider acceptance
        or authorizes later selected summary reservation.
        """
        if type(input_id) is not str or not re.fullmatch(r"[0-9a-f]{32}", input_id):
            raise CompactionJournalError("Exact private raw input ID required")
        canonical = str(session_file.resolve(strict=False))
        try:
            with (
                InputDispositions(
                    self.path.parent / InputDispositions.filename
                ).reading() as inputs,
                self._transaction() as db,
            ):
                if CompactionOperation.select(
                    db,
                    where=(
                        "session_file=? AND json_extract(state, '$.kind') IN "
                        f"{sql_names(OperationState, unresolved=True)} LIMIT 1"
                    ),
                    parameters=(canonical,),
                ) or self._blocking_selected_summary(db, canonical, inputs):
                    raise CompactionJournalError(
                        "Selected or unresolved journal blocks native input"
                    )
                PrivateRawInput(input_id, canonical).insert(db)
        except sqlite3.IntegrityError as error:
            raise CompactionJournalError("Private raw input ID already reserved") from error

    @contextmanager
    def ordinary_input_send_fence(
        self, session_file: Path, *, private_input_id: str | None = None
    ) -> Iterator[None]:
        """Exclude selected rows and unresolved commits through a raw stdin write.

        The caller must already hold the authoritative shared wire lock. This
        journal BEGIN IMMEDIATE serializes even a concurrent direct reservation
        that did not take the wire lock; an incomplete selected attempt denies
        before bytes can enter the native pipe. The exact saved file, not a
        recipient-wide prefix or a post-send cursor, is the exclusion key.
        """
        # A fresh Pi get_state may name its future .jsonl before writing a
        # header. A reservation requires an existing file; the journal lock
        # excludes a newly created/reserved file through the raw write too.
        canonical = str(session_file.resolve(strict=False))
        private_sessions = (self.path.parent / "native-sessions").resolve(strict=False)
        if Path(canonical).is_relative_to(private_sessions) and private_input_id is None:
            raise CompactionJournalError("Private raw send requires durable prewrite marker")
        with (
            InputDispositions(self.path.parent / InputDispositions.filename).reading() as inputs,
            self._transaction() as db,
        ):
            if CompactionOperation.select(
                db,
                where=(
                    "session_file=? AND json_extract(state, '$.kind') IN "
                    f"{sql_names(OperationState, unresolved=True)} LIMIT 1"
                ),
                parameters=(canonical,),
            ) or self._blocking_selected_summary(db, canonical, inputs):
                raise CompactionJournalError("Selected or unresolved journal blocks native input")
            if private_input_id is not None and PrivateRawInput.one(
                db, input_id=private_input_id
            ) != PrivateRawInput(private_input_id, canonical):
                raise CompactionJournalError("Exact durable private raw prewrite marker required")
            yield

    def refuse_selected_summary(self, operation_id: str, reason: str) -> None:
        """Retain the observed native prestart failure without admitting any input."""
        with self._transaction() as db:
            row = SelectedSummaryAttempt.one(db, operation_id=operation_id)
            if row is None:
                raise CompactionJournalError("Selected summary refusal transition forbidden")
            target = row.state.refuse(reason)
            SelectedSummaryAttempt.update(
                db, where="operation_id=?", parameters=(operation_id,), state=target
            )

    def retire_refused_summary(self, attempt: SelectedSummaryAttempt) -> None:
        """Explicitly retire a known refusal without admitting its original input."""
        target = attempt.state.manual_recovery()
        with self._transaction() as db:
            row = SelectedSummaryAttempt.one(db, operation_id=attempt.operation_id)
            if row != attempt:
                raise CompactionJournalError("Native refusal changed before explicit retirement")
            SelectedSummaryAttempt.update(
                db, where="operation_id=?", parameters=(attempt.operation_id,), state=target
            )

    def retire_unchanged_summary(self, attempt: SelectedSummaryAttempt) -> None:
        """Bridge holds the native writer and exact owner/source/input fences."""
        target = attempt.state.retire_unchanged_source()
        with self._transaction() as db:
            if SelectedSummaryAttempt.one(db, operation_id=attempt.operation_id) != attempt:
                raise CompactionJournalError("Selected summary changed during reconciliation")
            # Any commit intent is a different uncertainty domain. Even a
            # terminal commit must be reconciled by its native commit owner.
            if CompactionOperation.select(
                db,
                where=(
                    "session_file=? AND "
                    "json_extract(intent_json, '$.selectedSummaryOperationId')=?"
                ),
                parameters=(attempt.session_file, attempt.operation_id),
            ):
                raise CompactionJournalError("Native commit intent prevents summary retirement")
            SelectedSummaryAttempt.update(
                db, where="operation_id=?", parameters=(attempt.operation_id,), state=target
            )

    def selected_summaries(self, session_file: str) -> tuple[SelectedSummaryAttempt, ...]:
        """Inspect every recorded result without exposing model or input content."""
        canonical = str(Path(session_file).resolve(strict=True))
        with self._transaction() as db:
            return tuple(
                SelectedSummaryAttempt.select(
                    db, where="session_file=? ORDER BY rowid", parameters=(canonical,)
                )
            )

    def mark_selected_summary_unknown(self, operation_id: str) -> None:
        """Record transport uncertainty; never erase or retry the reservation."""
        with self._transaction() as db:
            row = SelectedSummaryAttempt.one(db, operation_id=operation_id)
            if row is None or not row.state.may_become(UnknownSummary()):
                raise CompactionJournalError("Selected summary uncertainty transition forbidden")
            SelectedSummaryAttempt.update(
                db, where="operation_id=?", parameters=(operation_id,), state=UnknownSummary()
            )

    def fail_selected_summary(self, operation_id: str, reason: str) -> None:
        """Record the selected child's correlated no-write failure, never a send ACK."""
        with self._transaction() as db:
            row = SelectedSummaryAttempt.one(db, operation_id=operation_id)
            if row is None:
                raise CompactionJournalError("Unknown selected summary")
            SelectedSummaryAttempt.update(
                db, where="operation_id=?", parameters=(operation_id,), state=row.state.fail(reason)
            )

    def decline_selected_summary_prestart(
        self,
        operation_id: str,
        reason: str,
        *,
        admission: SelectedAdmissionIdentity | None = None,
    ) -> SelectedSummaryAdmission | None:
        """Settle only an exact, verified clean Pi response before any side effect.

        Only split-turn or explicitly unsupported cuts may be *recorded* as
        clean pre-start declines. Even these remain blocked at the final input
        gate until separately reviewed exact-ID recovery exists. Busy, changed
        source/model/settings, timeout, transport loss, and post-auth/stream
        errors remain blocking.
        The future owner caller must verify the correlated Pi reply and current
        owner/ingress source before invoking this method; the journal is not
        that authority or evidence verifier.
        """
        try:
            target = DeclinedPrestartSummary(reason)
        except ValueError as error:
            raise CompactionJournalError(str(error)) from error
        with self._transaction() as db:
            row = SelectedSummaryAttempt.one(db, operation_id=operation_id)
            if row is None or not row.state.may_become(target):
                raise CompactionJournalError("Selected summary prestart decline forbidden")
            before = db.total_changes
            SelectedSummaryAttempt.update(
                db,
                where="operation_id=? AND json_extract(state, '$.kind')=?",
                parameters=(operation_id, ReservedSummary.declared_name),
                state=target,
            )
            after = SelectedSummaryAttempt.one(db, operation_id=operation_id)
            if db.total_changes != before + 1 or after != replace(row, state=target):
                raise CompactionJournalError("Exact clean decline transition required")
        if admission is not None:
            from .selected_summary_admission import SelectedSummaryAdmission

            # This method's verified clean-decline SQL is the only issuer.
            # _transaction() has already returned COMMIT + parent-fsync ACK.
            scope = (str(self.path), row.session_file, operation_id, target, row.source_json)
            receipt = _ReturnedTerminalAck()
            _issued_selected_acks[receipt] = scope
            return SelectedSummaryAdmission._from_returned_ack(
                receipt,
                self.path,
                row.session_file,
                operation_id,
                target,
                row.source_json,
                admission,
            )
        return None

    def link_selected_summary_commit(
        self,
        operation_id: str,
        commit_id: str,
        *,
        admission: SelectedAdmissionIdentity | None = None,
        state_type: type[LinkedSummary] = LinkedSummary,
    ) -> SelectedSummaryAdmission | None:
        """Settle only a reserved attempt after its exact native commit is durable.

        This is NOT a provider receipt validator or an input admission grant.
        A linked row remains a blocker until its exact original input has
        native-start evidence. The owner checks the complete selected Pi result
        and current owner/ingress source before the native CAS, and calls this
        only after the native journal committed.
        An UNKNOWN provider attempt cannot be automatically linked or retried.
        """
        target = state_type(commit_id)
        if admission is not None and not target.original_eligible:
            raise CompactionJournalError("Manual compaction cannot admit an original input")
        with self._transaction() as db:
            row = SelectedSummaryAttempt.one(db, operation_id=operation_id)
            commit = CompactionOperation.one(db, commit_id=commit_id)
            try:
                bound = commit is not None and (
                    json.loads(commit.intent_json).get("selectedSummaryOperationId") == operation_id
                )
            except (TypeError, ValueError, AttributeError):
                bound = False
            if (
                row is None
                or not row.state.may_become(target)
                or commit is None
                or commit.session_file != row.session_file
                or not commit.state.committed
                or not bound
            ):
                raise CompactionJournalError("Exact committed native result required to link")
            if (
                admission is not None
                and json.loads(commit.intent_json).get("selectedSummarySourceDigest")
                != TextDigest.of(row.source_json).value
            ):
                raise CompactionJournalError("Selected native intent source digest required")
            before = db.total_changes
            SelectedSummaryAttempt.update(
                db,
                where="operation_id=? AND json_extract(state, '$.kind')=?",
                parameters=(operation_id, ReservedSummary.declared_name),
                state=target,
            )
            after = SelectedSummaryAttempt.one(db, operation_id=operation_id)
            if db.total_changes != before + 1 or after != replace(row, state=target):
                raise CompactionJournalError("Exact committed native link required")
        if admission is not None:
            from .selected_summary_admission import SelectedSummaryAdmission

            # Only this method's verified native-link SQL can mint on returned fsync.
            scope = (str(self.path), row.session_file, operation_id, target, row.source_json)
            receipt = _ReturnedTerminalAck()
            _issued_selected_acks[receipt] = scope
            return SelectedSummaryAdmission._from_returned_ack(
                receipt,
                self.path,
                row.session_file,
                operation_id,
                target,
                row.source_json,
                admission,
            )
        return None

    def pending_publications(self, session_file: str) -> tuple[CompactionPublication, ...]:
        """Read exact-ID metadata; an unknown commit cannot be projected."""
        canonical = str(Path(session_file).resolve(strict=True))
        with self._transaction() as db:
            rows = CompactionPublication.select(
                db,
                where="session_file=? AND json_extract(state, '$.kind')=? ORDER BY rowid LIMIT 32",
                parameters=(canonical, PendingPublication.declared_name),
            )
            for row in rows:
                operation = CompactionOperation.one(db, commit_id=row.commit_id)
                try:
                    if (
                        operation is None
                        or not operation.state.committed
                        or row.session_file != operation.session_file
                        or len(row.metadata_json.encode()) > 4096
                        or row.metadata_json
                        != _publication_metadata(row.commit_id, json.loads(operation.evidence_json))
                    ):
                        raise ValueError("Changed publication")
                except (ValueError, TypeError, KeyError, AttributeError) as error:
                    raise CompactionJournalError(
                        "Compaction publication metadata changed"
                    ) from error
        return tuple(rows)

    def observe_publication(self, commit_id: str, metadata_json: str) -> None:
        """ACK only the exact metadata seen by the local ACP projection.

        Delivery must return before this call. A failure before marking leaves
        the row pending. A post-COMMIT fsync error may leave it *observed* even
        though this call raises UNKNOWN; reconcile the exact row, never infer
        native retry authority. Reprojection may repeat a pending commit ID, so
        consumers must deduplicate by ID, never by summary text.
        """
        with self._transaction() as db:
            row = CompactionPublication.one(db, commit_id=commit_id)
            if row is None or row.metadata_json != metadata_json:
                raise CompactionJournalError("Unknown or changed publication metadata")
            if row.state.may_become(ObservedPublication()):
                CompactionPublication.update(
                    db, where="commit_id=?", parameters=(commit_id,), state=ObservedPublication()
                )

    def resolve(
        self, commit_id: str, outcome: OperationState, evidence: dict, *, publication: bool = False
    ) -> None:
        """Persist bridge-validated native evidence; this does not verify it.

        Unknown/intent remains blocking. A terminal outcome is immutable.
        ``refused`` is only allowed directly after intent, for a proven
        pre-write native refusal. Once UNKNOWN, only writer-fenced exact-ID
        reconciliation may establish committed or aborted-no-write.
        """
        metadata = _publication_metadata(commit_id, evidence) if publication else None
        if publication and not outcome.committed:
            raise CompactionJournalError("Exact committed native metadata required")
        payload = json.dumps(evidence, sort_keys=True, separators=(",", ":"), allow_nan=False)
        if len(payload.encode()) > 65536:
            raise ValueError("Compaction outcome exceeds bound")
        with self._transaction() as db:
            row = CompactionOperation.one(db, commit_id=commit_id)
            if row is None or not row.state.may_become(outcome):
                raise CompactionJournalError("Compaction outcome transition forbidden")
            CompactionOperation.update(
                db,
                where="commit_id=?",
                parameters=(commit_id,),
                state=outcome,
                evidence_json=payload,
            )
            if metadata is not None:
                CompactionPublication(
                    commit_id, row.session_file, metadata, PendingPublication()
                ).insert(db)
