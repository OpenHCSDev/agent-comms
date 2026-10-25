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
from .thread_identity import ClaimedIdentity, GenerationCounter

if TYPE_CHECKING:
    from .native_session_reopen import NativeSessionIdentity


class NativeClaimEpoch(DeclaredFamily, JsonShapeFamily, affix="NativeClaim"):
    @abstractmethod
    def matches(self, claimed_generation: int) -> bool: ...

    @abstractmethod
    def reservation_violation(self) -> bool: ...

    def record(self, row, db, claimed_generation: int, session: NativeSessionIdentity) -> None:
        raise StaleFence("native input claim_scope was already bound")

    @abstractmethod
    def require_release(self, acknowledgement, snapshot, current) -> None: ...

    @abstractmethod
    def recovery_session_files(self, row, stopped_owner) -> tuple[Path, ...]:
        """Find session files for the input's selection or the stopped process's selection."""


@dataclass(frozen=True)
class UnrecordedNativeClaim(NativeClaimEpoch, JsonShapeMember):
    # Represent SQL NULL explicitly, rather than using an optional send state.
    value: None = None

    def matches(self, claimed_generation: int) -> bool:
        return False

    def reservation_violation(self) -> bool:
        return False

    def record(self, row, db, claimed_generation: int, session: NativeSessionIdentity) -> None:
        updated = row.update(
            db,
            where="input_id=? AND {sent_owner_claimed_generation} IS NULL",
            parameters=(row.input_id,),
            sent_owner_claimed_generation=RecordedNativeClaim(claimed_generation),
            session_id=session.session_id,
            session_file=session.session_file,
        )
        if updated.rowcount != 1:
            raise StaleFence("native input claim_scope was already bound")

    def require_release(self, acknowledgement, snapshot, current) -> None:
        # No sending claim was recorded. OwnerRelease has already checked the
        # original process; no successor claim or turn can prove more about it.
        pass

    def recovery_session_files(self, row, stopped_owner) -> tuple[Path, ...]:
        # VerifiedOwnerLoss holds the registry boundary while observing resources.
        return tuple(files.session for files in stopped_owner.runtime.storage_files())


@dataclass(frozen=True)
class RecordedNativeClaim(NativeClaimEpoch, JsonShapeMember):
    value: int

    def __post_init__(self) -> None:
        GenerationCounter.require_positive(self.value)

    def matches(self, claimed_generation: int) -> bool:
        return self.value == claimed_generation

    def reservation_violation(self) -> bool:
        return True

    def require_release(self, acknowledgement, snapshot, current) -> None:
        sent = ClaimedIdentity(current.incarnation, self.value)
        fence = ClaimedIdentity(acknowledgement.incarnation(), acknowledgement.before)
        if not fence.includes(sent):
            raise RelationViolationError("Native release predates the sending claim_scope")
        if snapshot.claim_generation(current.name) == acknowledgement.after:
            if not acknowledgement.current(snapshot, current, acknowledgement.before):
                raise RelationViolationError("Native release differs from its stopped declaration")

    def recovery_session_files(self, row, stopped_owner) -> tuple[Path, ...]:
        # The original send contract authorized only journals inside this
        # recipient's native-sessions directory, and recorded selected identity
        # later, with the context acknowledgement. That directory remains checked by
        # VerifiedOwnerLoss. New send grants atomically record the selected file;
        # keep its additional subprocess check whenever it exists. Neither
        # representation permits borrowing the replacement owner's selection.
        return tuple(session.path for session in row.selected_session.present())
