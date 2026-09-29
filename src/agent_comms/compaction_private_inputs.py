"""Fresh private enrollment and journal-fenced ordinary input writes."""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING
from weakref import WeakKeyDictionary

from .child_process import ProcessIdentity
from .compaction_errors import CompactionJournalError
from .compaction_identity import (
    ReturnedFreshEnrollment,
)
from .compaction_journal_role import JournalRole
from .compaction_records import (
    CompactionOperation,
    EnrolledPrivateSession,
    PrivateRawInput,
    SelectedSummaryAttempt,
    SessionJournalHistory,
)
from .field_codec import FieldCodec
from .input_disposition import InputDispositions, InputDocument
from .selected_source import SelectedSource
from .thread_identity import GenerationCounter, ThreadIncarnation

if TYPE_CHECKING:
    from .fresh_private_session import FreshPrivateSession

# Only a returned COMMIT+fsync enrolls a live creation object; a visible row cannot.
_returned_fresh_enrollments: WeakKeyDictionary[FreshPrivateSession, ReturnedFreshEnrollment] = (
    WeakKeyDictionary()
)


class PrivateInputs(JournalRole):
    def enroll(
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
        try:
            owner_lookup = FieldCodec.decode(str, owner_lookup)
            GenerationCounter.require_positive(owner_generation)
            GenerationCounter.require_positive(admission_generation)
        except (ValueError, TypeError) as error:
            raise CompactionJournalError(
                "Fresh-session owner or private location differs"
            ) from error
        if not owner_lookup or "/" in owner_lookup:
            raise CompactionJournalError("Exact private owner lookup required")
        private_root = (self.journal.path.parent / "native-sessions").resolve(strict=False)
        if fresh.path.resolve(strict=True).parent != private_root / owner_lookup:
            raise CompactionJournalError("Fresh-session owner or private location differs")
        try:
            with self.journal.transaction() as db:
                SessionJournalHistory.require_pristine(db, str(fresh.path))
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
        _returned_fresh_enrollments[fresh] = ReturnedFreshEnrollment(self.journal.path, enrollment)

    def reserve(self, session_file: Path, input_id: str) -> None:
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
                    self.journal.path.parent / InputDispositions.filename
                ).reading() as inputs,
                self.journal.transaction() as db,
            ):
                self.require_clear(db, canonical, inputs)
                PrivateRawInput(input_id, canonical).insert(db)
        except sqlite3.IntegrityError as error:
            raise CompactionJournalError("Private raw input ID already reserved") from error

    @contextmanager
    def send_fence(
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
        private_sessions = (self.journal.path.parent / "native-sessions").resolve(strict=False)
        if Path(canonical).is_relative_to(private_sessions) and private_input_id is None:
            raise CompactionJournalError("Private raw send requires durable prewrite marker")
        with (
            InputDispositions(
                self.journal.path.parent / InputDispositions.filename
            ).reading() as inputs,
            self.journal.transaction() as db,
        ):
            self.require_clear(db, canonical, inputs)
            if private_input_id is not None and PrivateRawInput.one(
                db, input_id=private_input_id
            ) != PrivateRawInput(private_input_id, canonical):
                raise CompactionJournalError("Exact durable private raw prewrite marker required")
            yield

    def require_coverage(
        self,
        enrollment: EnrolledPrivateSession,
        fresh: FreshPrivateSession,
        witness: SelectedSource,
        admission_generation: int | None,
    ) -> None:
        if _returned_fresh_enrollments.get(fresh) != ReturnedFreshEnrollment(
            self.journal.path, enrollment
        ):
            raise CompactionJournalError(
                "Fresh private owner coverage differs: no returned enrollment"
            )
        enrollment.require_coverage(fresh, witness, admission_generation)

    @staticmethod
    def require_clear(db: sqlite3.Connection, canonical: str, inputs: InputDocument) -> None:
        """Both raw-input phases exclude the same unresolved and original-input barriers."""
        if CompactionOperation.unresolved_in(db, canonical) or SelectedSummaryAttempt.blocking_in(
            db, canonical, inputs
        ):
            raise CompactionJournalError("Selected or unresolved journal blocks native input")
