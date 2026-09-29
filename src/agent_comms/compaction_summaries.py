"""Selected attempt lifecycle and post-fsync one-use terminal authority."""

from __future__ import annotations

import json
import re
import sqlite3
import stat
from pathlib import Path
from typing import TYPE_CHECKING
from uuid import uuid4
from weakref import WeakKeyDictionary

from .compaction_errors import CompactionJournalError
from .compaction_identity import (
    ReturnedSummaryTerminal,
)
from .compaction_journal_role import JournalRole
from .compaction_records import (
    CompactionOperation,
    EnrolledPrivateSession,
    PrivateRawInput,
    SelectedSummaryAttempt,
    SelectedSummarySource,
)
from .compaction_states import (
    DeclinedPrestartSummary,
    LinkedSummary,
    ReservedSummary,
    UnknownSummary,
)
from .field_codec import FieldCodec
from .input_disposition import FutureInputQueue, InputDispositions
from .store_files import _store_lock

if TYPE_CHECKING:
    from .fresh_private_session import FreshPrivateSession
    from .selected_summary_admission import SelectedAdmissionIdentity, SelectedSummaryAdmission


class _ReturnedTerminalAck:
    """Only the successful post-COMMIT parent fsync creates this identity."""

    __slots__ = ("__weakref__",)


_issued_selected_acks: WeakKeyDictionary[_ReturnedTerminalAck, ReturnedSummaryTerminal] = (
    WeakKeyDictionary()
)


def _consume_selected_ack(receipt: object, expected: ReturnedSummaryTerminal) -> bool:
    """Consume exact one-use post-fsync receipt, never derive one from a row."""
    if type(receipt) is not _ReturnedTerminalAck:
        return False
    recorded = _issued_selected_acks.pop(receipt, None)
    return recorded == expected


class SelectedSummaries(JournalRole):
    def reserve(
        self,
        session_file: str,
        source: dict,
        *,
        operation_id: str | None = None,
        fresh_session: FreshPrivateSession | None = None,
        admission_generation: int | None = None,
        future_queue: FutureInputQueue | None = None,
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
        private_sessions = (self.journal.path.parent / "native-sessions").resolve(strict=False)
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

        try:
            with _store_lock(self.journal.path.parent / "wire"):
                if future_queue is not None:
                    from .registration import Registration

                    owner, _ = Registration(
                        self.journal.path.parent / "registry.json"
                    ).live_owner_with_generation(envelope.source.incarnation.name)
                with (
                    InputDispositions(
                        self.journal.path.parent / InputDispositions.filename
                    ).reading() as inputs,
                    self.journal.transaction() as db,
                ):
                    envelope.source.reservation_check(
                        _session_revision(canonical), inputs
                    ).require_valid()
                    covered_inputs = (
                        future_queue.compaction_inputs(envelope.source, owner, inputs)
                        if future_queue is not None
                        else inputs
                    )
                    if private and fresh_session is not None:
                        assert fresh_session is not None
                        coverage = EnrolledPrivateSession.one(db, session_file=canonical)
                        if coverage is None:
                            raise CompactionJournalError(
                                "Fresh private owner coverage differs: not enrolled"
                            )
                        self.journal.private_inputs.require_coverage(
                            coverage, fresh_session, envelope.source, admission_generation
                        )
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
                                self.journal.path.parent,
                                Path(canonical),
                                envelope.source,
                                raw_ids,
                                covered_inputs,
                            )
                        except (OSError, ValueError, sqlite3.Error, RuntimeError) as error:
                            raise CompactionJournalError(
                                "Private selected reservation requires reviewed "
                                "raw-history coverage floor"
                            ) from error
                    if CompactionOperation.unresolved_in(db, canonical):
                        raise CompactionJournalError(
                            "Unresolved native commit; no selected summary"
                        )
                    if SelectedSummaryAttempt.blocking_in(db, canonical, inputs) or (
                        raw_ids and (not private or fresh_session is not None)
                    ):
                        raise CompactionJournalError("Blocked selected summary; never replay")
                    SelectedSummaryAttempt(
                        operation_id, canonical, payload, ReservedSummary()
                    ).insert(db)
        except sqlite3.IntegrityError as error:
            raise CompactionJournalError(
                "Blocked selected summary or reused operation ID; never replay"
            ) from error
        return operation_id

    def get(self, operation_id: str) -> SelectedSummaryAttempt:
        with self.journal.transaction() as db:
            row = SelectedSummaryAttempt.one(db, operation_id=operation_id)
        if row is None:
            raise CompactionJournalError("Unknown selected summary operation")
        return row

    def require_current(self, attempt: SelectedSummaryAttempt, session_file: str) -> None:
        if self.get(attempt.operation_id) != attempt:
            raise CompactionJournalError("Selected summary reservation changed before commit")
        attempt.require_session(session_file)

    def unresolved(self, session_file: str) -> tuple[SelectedSummaryAttempt, ...]:
        """Crash-orphaned reservations block every subsequent input send."""
        canonical = str(Path(session_file).resolve(strict=True))
        with self.journal.transaction() as db:
            rows = SelectedSummaryAttempt.unresolved_in(db, canonical)
        return tuple(rows)

    def blocking(self, session_file: str) -> tuple[SelectedSummaryAttempt, ...]:
        """Block until the exact original input has durable native-start evidence.

        Terminal-looking summary rows never grant a send or mint a token. Once
        the original has actually started, the existing disposition prevents
        replay while ordinary new inputs and later compactions can proceed.
        """
        canonical = str(Path(session_file).resolve(strict=True))
        with (
            InputDispositions(
                self.journal.path.parent / InputDispositions.filename
            ).reading() as inputs,
            self.journal.transaction() as db,
        ):
            return SelectedSummaryAttempt.blocking_in(db, canonical, inputs)

    def refuse(self, operation_id: str, reason: str) -> None:
        """Retain the observed native prestart failure without admitting any input."""
        with self.journal.transaction() as db:
            row = SelectedSummaryAttempt.one(db, operation_id=operation_id)
            if row is None:
                raise CompactionJournalError("Selected summary refusal transition forbidden")
            target = row.state.refuse(reason)
            row.transition(db, target)

    def retire_refused(self, attempt: SelectedSummaryAttempt) -> None:
        """Explicitly retire a known refusal without admitting its original input."""
        target = attempt.state.manual_recovery()
        with self.journal.transaction() as db:
            row = SelectedSummaryAttempt.one(db, operation_id=attempt.operation_id)
            if row != attempt:
                raise CompactionJournalError("Native refusal changed before explicit retirement")
            attempt.transition(db, target)

    def retire_unchanged(self, attempt: SelectedSummaryAttempt) -> None:
        """Bridge holds the native writer and exact owner/source/input fences."""
        target = attempt.state.retire_unchanged_source()
        with self.journal.transaction() as db:
            if SelectedSummaryAttempt.one(db, operation_id=attempt.operation_id) != attempt:
                raise CompactionJournalError("Selected summary changed during reconciliation")
            # Any commit intent is a different uncertainty domain. Even a
            # terminal commit must be reconciled by its native commit owner.
            if CompactionOperation.select(
                db,
                where=(
                    "session_file=? AND json_extract(intent_json, '$.selectedSummaryOperationId')=?"
                ),
                parameters=(attempt.session_file, attempt.operation_id),
            ):
                raise CompactionJournalError("Native commit intent prevents summary retirement")
            attempt.transition(db, target)

    def history(self, session_file: str) -> tuple[SelectedSummaryAttempt, ...]:
        """Inspect every recorded result without exposing model or input content."""
        canonical = str(Path(session_file).resolve(strict=True))
        with self.journal.transaction() as db:
            return tuple(
                SelectedSummaryAttempt.select(
                    db, where="session_file=? ORDER BY rowid", parameters=(canonical,)
                )
            )

    def mark_unknown(self, operation_id: str) -> None:
        """Record transport uncertainty; never erase or retry the reservation."""
        with self.journal.transaction() as db:
            row = SelectedSummaryAttempt.one(db, operation_id=operation_id)
            if row is None or not row.state.may_become(UnknownSummary()):
                raise CompactionJournalError("Selected summary uncertainty transition forbidden")
            row.transition(db, UnknownSummary())

    def fail(self, operation_id: str, reason: str) -> None:
        """Record the selected child's correlated no-write failure, never a send ACK."""
        with self.journal.transaction() as db:
            row = SelectedSummaryAttempt.one(db, operation_id=operation_id)
            if row is None:
                raise CompactionJournalError("Unknown selected summary")
            row.transition(db, row.state.fail(reason))

    def decline_prestart(
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
        with self.journal.transaction() as db:
            row = SelectedSummaryAttempt.one(db, operation_id=operation_id)
            if row is None or not row.state.may_become(target):
                raise CompactionJournalError("Selected summary prestart decline forbidden")
            terminal = row.transition(db, target)
        if admission is not None:
            from .selected_summary_admission import SelectedSummaryAdmission

            # This method's verified clean-decline SQL is the only issuer.
            # transaction() has already returned COMMIT + parent-fsync ACK.
            scope = ReturnedSummaryTerminal(self.journal.path, terminal)
            receipt = _ReturnedTerminalAck()
            _issued_selected_acks[receipt] = scope
            return SelectedSummaryAdmission._from_returned_ack(
                receipt,
                scope,
                admission,
            )
        return None

    def link_commit(
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
        if admission is not None:
            target.require_original_admission()
        with self.journal.transaction() as db:
            row = SelectedSummaryAttempt.one(db, operation_id=operation_id)
            commit = CompactionOperation.one(db, commit_id=commit_id)
            if row is None or commit is None:
                raise CompactionJournalError("Exact committed native result required to link")
            row.require_transition(target)
            commit.require_summary_link(row, admit_original=admission is not None)
            terminal = row.transition(db, target)
        if admission is not None:
            from .selected_summary_admission import SelectedSummaryAdmission

            # Only this method's verified native-link SQL can mint on returned fsync.
            scope = ReturnedSummaryTerminal(self.journal.path, terminal)
            receipt = _ReturnedTerminalAck()
            _issued_selected_acks[receipt] = scope
            return SelectedSummaryAdmission._from_returned_ack(
                receipt,
                scope,
                admission,
            )
        return None

    def require_original_admission(self, expected: SelectedSummaryAttempt) -> None:
        if self.get(expected.operation_id) != expected:
            raise CompactionJournalError("Selected terminal record changed")
        if len(self.blocking(expected.session_file)) != 1:
            raise CompactionJournalError("Selected original has competing reservations")
        if self.journal.operations.unresolved(expected.session_file):
            raise CompactionJournalError("Unresolved native commit excludes selected input")
        if not expected.state.verifies_original(self.journal, expected):
            raise CompactionJournalError("Selected terminal does not admit original input")
