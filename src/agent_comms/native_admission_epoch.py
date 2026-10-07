"""Original SQLite send epoch owns admission and released-owner behavior.

NULL and a positive integer are the original column's storage representations.
FieldCodec decodes that boundary once into these explicit admission members.
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
    def matches(self, admission_generation: int) -> bool: ...

    @abstractmethod
    def reservation_violation(self) -> bool: ...

    def record(self, row, db, admission_generation: int, session: NativeSessionIdentity) -> None:
        raise StaleFence("native input admission was already bound")

    @abstractmethod
    def require_release(self, receipt, snapshot, current) -> None: ...

    @abstractmethod
    def recovery_session_files(self, row, receipt) -> tuple[Path, ...]:
        """Original launch selections to inspect after attested owner loss."""


@dataclass(frozen=True)
class UnrecordedNativeAdmission(NativeAdmissionEpoch, JsonShapeMember):
    # Explicit SQL NULL representation, not an optional internal admission state.
    value: None = None

    def matches(self, admission_generation: int) -> bool:
        return False

    def reservation_violation(self) -> bool:
        return False

    def record(self, row, db, admission_generation: int, session: NativeSessionIdentity) -> None:
        updated = row.update(
            db,
            where="input_id=? AND sent_owner_admission_generation IS NULL",
            parameters=(row.input_id,),
            sent_owner_admission_generation=RecordedNativeAdmission(admission_generation),
            session_id=session.session_id,
            session_file=session.session_file,
        )
        if updated.rowcount != 1:
            raise StaleFence("native input admission was already bound")

    def require_release(self, receipt, snapshot, current) -> None:
        if not receipt.current(snapshot, current, receipt.before):
            raise RelationViolationError("Unrecorded native send has no exact stopped release")

    def recovery_session_files(self, row, receipt) -> tuple[Path, ...]:
        # Selection can launch before get_state/send admission records a session.
        # The exact stopped declaration still owns an existing saved selection;
        # an unopened owner instead has only its allocated session directory.
        saved = receipt.thread.session_file
        return () if saved is None else (Path(saved),)


@dataclass(frozen=True)
class RecordedNativeAdmission(NativeAdmissionEpoch, JsonShapeMember):
    value: int

    def __post_init__(self) -> None:
        GenerationCounter.require_positive(self.value)

    def matches(self, admission_generation: int) -> bool:
        return self.value == admission_generation

    def reservation_violation(self) -> bool:
        return True

    def require_release(self, receipt, snapshot, current) -> None:
        sent = AdmissionIdentity(current.incarnation, self.value)
        fence = AdmissionIdentity(receipt.thread.incarnation, receipt.before)
        if not fence.includes(sent):
            raise RelationViolationError("Native release predates the sending admission")

    def recovery_session_files(self, row, receipt) -> tuple[Path, ...]:
        return (row.require_session_identity().path,)
