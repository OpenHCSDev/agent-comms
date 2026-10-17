"""The recorded SQLite send generation determines send and released-owner behavior.

NULL and a positive integer are the original column's storage representations.
FieldCodec decodes that boundary once into these explicit send states.
Neither an absent epoch nor a later release proves that prompt bytes were unwritten.
"""

from __future__ import annotations

from abc import abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from .coordination_errors import StaleFence
from .declared_family import DeclaredFamily
from .errors import RelationViolationError
from .field_codec import JsonShapeFamily, JsonShapeMember
from .thread_identity import AdmissionIdentity, GenerationCounter

if TYPE_CHECKING:
    from .native_session_reopen import NativeSessionIdentity


class NativeAdmissionEpoch(DeclaredFamily, JsonShapeFamily, affix="NativeAdmission"):
    @abstractmethod
    def matches(self, claimed_generation: int) -> bool: ...

    @abstractmethod
    def reservation_violation(self) -> bool: ...

    def record(self, row, db, claimed_generation: int, session: NativeSessionIdentity) -> None:
        raise StaleFence("native input admission was already bound")

    @abstractmethod
    def require_release(self, receipt, snapshot, current) -> None: ...

    @abstractmethod
    def recovery_session_files(self, row, receipt) -> tuple[Path, ...]:
        """Original launch selections to inspect after attested owner loss."""


@dataclass(frozen=True)
class UnrecordedNativeAdmission(NativeAdmissionEpoch, JsonShapeMember):
    # Represent SQL NULL explicitly, rather than using an optional send state.
    value: None = None

    def matches(self, claimed_generation: int) -> bool:
        return False

    def reservation_violation(self) -> bool:
        return False

    def record(self, row, db, claimed_generation: int, session: NativeSessionIdentity) -> None:
        updated = row.update(
            db,
            where="input_id=? AND sent_owner_admission_generation IS NULL",
            parameters=(row.input_id,),
            sent_owner_admission_generation=RecordedNativeAdmission(claimed_generation),
            session_id=session.session_id,
            session_file=session.session_file,
        )
        if updated.rowcount != 1:
            raise StaleFence("native input admission was already bound")

    def require_release(self, receipt, snapshot, current) -> None:
        current.require_local_process(receipt.thread.require_process())
        current.require_idle()
        if not receipt.current(snapshot, current, receipt.before):
            raise RelationViolationError("Unrecorded native send has no exact stopped release")

    def recovery_session_files(self, row, stopped) -> tuple[Path, ...]:
        # An unopened owner has no stored runtime files to recover.
        return tuple(files.session for files in stopped.thread.runtime.storage_files())


@dataclass(frozen=True)
class RecordedNativeAdmission(NativeAdmissionEpoch, JsonShapeMember):
    value: int

    def __post_init__(self) -> None:
        GenerationCounter.require_positive(self.value)

    def matches(self, claimed_generation: int) -> bool:
        return self.value == claimed_generation

    def reservation_violation(self) -> bool:
        return True

    def require_release(self, receipt, snapshot, current) -> None:
        sent = AdmissionIdentity(current.incarnation, self.value)
        fence = AdmissionIdentity(receipt.thread.incarnation, receipt.before)
        if not fence.includes(sent):
            raise RelationViolationError("Native release predates the sending admission")
        if snapshot.claimed_generations[current.name] == receipt.after:
            if not receipt.current(snapshot, current, receipt.before):
                raise RelationViolationError("Native release differs from its stopped declaration")

    def recovery_session_files(self, row, receipt) -> tuple[Path, ...]:
        # The original send contract admitted only journals inside this
        # recipient's native-sessions directory, and recorded selected identity
        # later, with the context receipt. That directory remains checked by
        # VerifiedOwnerLoss. New send grants atomically record the selected file;
        # keep its additional subprocess check whenever it exists. Neither
        # representation permits borrowing the replacement owner's selection.
        session = row.selected_session
        return () if session is None else (session.path,)
