"""Fresh private enrollment and journal-fenced ordinary input writes."""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
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
    NativeForkCreation,
    PrivateRawInput,
    SelectedSummaryAttempt,
    SessionJournalHistory,
)
from .field_codec import FieldCodec
from .diagnostics import PublicationMeasurements
from .input_disposition import InputDispositions, InputDocument
from .selected_source import SelectedSource
from .private_path import FileRevision
from .thread_identity import GenerationCounter, ThreadIncarnation

if TYPE_CHECKING:
    from .native_entries import NativeEvidenceRead
    from .fresh_private_session import FreshPrivateSession

# Only a returned COMMIT+fsync enrolls a live creation object; a visible row cannot.
_returned_fresh_enrollments: WeakKeyDictionary[FreshPrivateSession, ReturnedFreshEnrollment] = (
    WeakKeyDictionary()
)


class PrivateInputs(JournalRole):
    async def fork(self, request, *, cwd: Path, env=None):
        """Capture the returned SDK creation before exposing the child to input.

        Reading a header or passing an old receipt cannot enter this operation:
        it always invokes the canonical new-file fork once. Unknown creation or
        journal publication is never retried or inferred from an orphan file.
        """
        from .native_fork import ForkSessionHelper
        from .native_entries import NativeEntry

        created = await ForkSessionHelper.run(request, cwd=cwd, env=env)
        created.source.require_session(request.file)
        # Corroborate the whole inherited history before the journal's write
        # transaction: every owner's input reservation opens that same journal,
        # so it must not wait on reading a large fork. The transaction re-checks
        # the unchanged revision and publishes the creation.
        with NativeEntry.open_evidence(created.path) as evidence:
            _, entries = evidence.observe()
            created.covered_prefix(evidence, entries)
            with self.journal.transaction() as db:
                SessionJournalHistory.require_pristine(db, created.session_file)
                if FileRevision.from_stat(created.path.stat()) != created.revision:
                    raise CompactionJournalError("Native fork changed before creation publication")
                created.insert(db)
        return created

    def requires_raw_marker(self, session_file: Path) -> bool:
        """The journal's allocated writer namespace requires prewrite custody.

        This is a storage obligation, not a session selector or an enrollment.
        Receipts for sources outside that namespace still require coverage.
        """
        return session_file.resolve(strict=False).is_relative_to(
            (self.journal.path.parent / "native-sessions").resolve(strict=False)
        )

    def require_source_coverage(
        self,
        db: sqlite3.Connection,
        session_file: Path,
        source: SelectedSource,
        inputs: InputDocument,
        *,
        fresh: FreshPrivateSession | None,
        admission_generation: int | None,
        native_reader: NativeEvidenceRead | None = None,
    ) -> None:
        """The original private-input owner covers every selected raw input.

        A returned mint covers only its original enrollment. Continued source
        coverage comes from the original acquired source and corroborated
        live-recorded ancestry. Input receipts still prove their own deliveries,
        irrespective of today's routing name or the selected journal directory.
        No marker, file observation or enrollment row can mint fresh custody.
        """
        canonical = str(session_file)
        enrollment = EnrolledPrivateSession.one(db, session_file=canonical)
        raw_ids = frozenset(
            row.input_id for row in PrivateRawInput.select(
                db, where="session_file=?", parameters=(canonical,)
            )
        )
        if fresh is not None:
            from .fresh_private_session import FreshPrivateSession

            if type(fresh) is not FreshPrivateSession or enrollment is None:
                raise CompactionJournalError("Returned enrolled fresh source required")
            if fresh.path != session_file:
                raise CompactionJournalError("Fresh private selected identity changed")
            if admission_generation is not None:
                GenerationCounter.require_positive(admission_generation)
            self.require_coverage(enrollment, fresh, source, admission_generation)
            if raw_ids:
                raise CompactionJournalError("Fresh raw input remains UNKNOWN; never replay")
            return
        if SessionJournalHistory.exists(db, canonical) or self.requires_raw_marker(session_file):
            from .continued_private_session import verify_continued_private_session

            try:
                verify_continued_private_session(
                    self.journal.path.parent, session_file, source, raw_ids, inputs,
                    journal_db=db, native_reader=native_reader,
                )
            except (OSError, ValueError, sqlite3.Error, RuntimeError) as error:
                raise CompactionJournalError(
                    "Selected source requires reviewed raw-history coverage floor"
                ) from error

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
                    fresh.file_identity.device,
                    fresh.file_identity.inode,
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
        with self.admission(session_file) as admitted:
            admitted.mark_unknown(input_id)

    @contextmanager
    def admission(
        self, session_file: Path, *, blocking: bool = True,
        measurements: PublicationMeasurements | None = None,
    ) -> Iterator[PrivateInputSend]:
        """Acquire ALL input/journal custody before consuming raw admission.

        The original SQLite EXCLUSIVE connection retains its file lock across
        the durable UNKNOWN checkpoint. There is no post-token lock upgrade or
        second connection gap between that checkpoint and the raw send.
        """
        canonical = str(session_file.resolve(strict=False))
        observations = measurements if measurements is not None else PublicationMeasurements()
        with ExitStack() as custody:
            with observations.operation("original_input_document"):
                inputs = custody.enter_context(InputDispositions(
                    self.journal.path.parent / InputDispositions.filename
                ).reading(blocking=blocking))
            with observations.operation("compaction_journal_exclusion"):
                db = custody.enter_context(self.journal.transaction(
                    blocking=blocking, retain_exclusion=True))
            with observations.operation("raw_source_clearance"):
                self.require_clear(db, canonical, inputs)
            yield PrivateInputSend(self.journal, db, canonical, inputs)

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
        if self.requires_raw_marker(Path(canonical)) and private_input_id is None:
            raise CompactionJournalError("Private raw send requires durable prewrite marker")
        with self.admission(session_file) as admitted:
            if private_input_id is not None:
                admitted.require_marker(private_input_id)
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


@dataclass
class PrivateInputSend(PrivateInputs):
    """Original held resources, not another input or admission authority."""

    db: sqlite3.Connection
    canonical: str
    inputs: InputDocument

    def mark_unknown(self, input_id: str) -> None:
        if type(input_id) is not str or not re.fullmatch(r"[0-9a-f]{32}", input_id):
            raise CompactionJournalError("Exact private raw input ID required")
        self.require_clear(self.db, self.canonical, self.inputs)
        try:
            PrivateRawInput(input_id, self.canonical).insert(self.db)
        except sqlite3.IntegrityError as error:
            raise CompactionJournalError("Private raw input ID already reserved") from error
        self.journal.commit(self.db)
        # locking_mode=EXCLUSIVE preserves the SAME SQLite file exclusion even
        # between transactions; this begins under custody already granted.
        self.db.execute("BEGIN EXCLUSIVE")
        self.require_marker(input_id)

    def require_marker(self, input_id: str) -> None:
        if PrivateRawInput.one(self.db, input_id=input_id) != PrivateRawInput(
            input_id, self.canonical
        ):
            raise CompactionJournalError("Exact durable private raw prewrite marker required")
