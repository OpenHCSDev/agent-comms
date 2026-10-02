"""Current selected-summary source records; journals reset at installation."""

from __future__ import annotations

from abc import abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from .child_process import ProcessIdentity
from .coordination_errors import StaleRevision
from .declared_family import DeclaredFamily
from .private_path import FileRevision
from .text_digest import TextDigest
from .thread_identity import ThreadIncarnation, TurnId

if TYPE_CHECKING:
    from .input_disposition import InputDocument
    from .reservation_rules import ReservationCheck

class SessionRevisionUnavailable(ValueError):
    """The selected filesystem observation cannot supply a revision."""


class InputProofRevision(DeclaredFamily, affix="ProofRevision"):
    """Observation of the original proof journal, never proof of an input."""


@dataclass(frozen=True, slots=True)
class MissingInputProofRevision(InputProofRevision):
    """The sidecar path is absent; no input-history or replay fact is inferred."""


@dataclass(frozen=True, slots=True)
class PresentInputProofRevision(InputProofRevision):
    revision: FileRevision


class SessionObservation(DeclaredFamily, affix="Observation"):
    def matches(self, reserved: SessionRevision) -> bool:
        return False

    @abstractmethod
    def require_available(self) -> SessionRevision: ...


@dataclass(frozen=True)
class UnavailableSessionObservation(SessionObservation):
    error: Exception

    def require_available(self) -> SessionRevision:
        raise SessionRevisionUnavailable(str(self.error)) from self.error


@dataclass(frozen=True, slots=True)
class SessionRevision(SessionObservation):
    """One native-file revision and its separately observed input-proof resource.

    Equality fences the whole reserved source. Outcome placement deliberately
    permits append on the same native inode; native compaction must preserve the
    proof observation. None of these observations acquires native custody.
    """

    native: FileRevision
    input_proof: InputProofRevision

    @classmethod
    def observe(cls, session_file: str | None) -> SessionObservation:
        if not isinstance(session_file, str) or not session_file:
            return UnavailableSessionObservation(ValueError("No selected native session"))
        try:
            native = FileRevision.from_stat(Path(session_file).stat())
            try:
                proof = PresentInputProofRevision(
                    FileRevision.from_stat(Path(session_file + ".input-proof").stat())
                )
            except FileNotFoundError:
                proof = MissingInputProofRevision()
            return cls(native, proof)
        except OSError as error:
            # Unreadable proof is not a missing proof. Deny source acquisition.
            return UnavailableSessionObservation(error)

    def require_available(self) -> SessionRevision:
        return self

    def matches(self, reserved: SessionRevision) -> bool:
        return self == reserved

    def current(self, session_file: str | None) -> bool:
        return self.observe(session_file).matches(self)

    def same_input_proof(self, reserved: SessionRevision) -> bool:
        """Compare sidecar observations, not committed native-input evidence."""
        return self.input_proof == reserved.input_proof

    def require_native_cut(self, session_file: str, through_offset: int) -> None:
        try:
            observed = self.observe(session_file).require_available()
        except SessionRevisionUnavailable as error:
            raise StaleRevision("Compaction outcome native source unavailable") from error
        if observed.native.identity != self.native.identity:
            raise StaleRevision("Compaction outcome native inode changed")
        if not self.native.size <= through_offset <= observed.native.size:
            raise StaleRevision("Compaction outcome native cut was truncated or not captured")


@dataclass(frozen=True, kw_only=True)
class SelectedSource(DeclaredFamily, affix="Source"):
    owner: ProcessIdentity
    incarnation: ThreadIncarnation
    turn: TurnId
    reserved_revision: SessionRevision

    def interrupted_check(self, revision, inputs, incarnation, turn):
        from .reservation_rules import InterruptedReservationCheck

        return InterruptedReservationCheck(
            source=self, revision=revision, incarnation=incarnation, turn=turn
        )

    def matches_pending_input(self, key: str | None) -> bool:
        return True

    def original_has_started(self, inputs: InputDocument) -> bool:
        return False

    @property
    @abstractmethod
    def pending_input_key(self) -> str | None: ...

    @abstractmethod
    def reservation_check(
        self, revision: SessionObservation, inputs: InputDocument
    ) -> ReservationCheck: ...


@dataclass(frozen=True, slots=True, kw_only=True)
class ManualSource(SelectedSource):
    @property
    def pending_input_key(self) -> None:
        return None

    def reservation_check(self, revision, inputs):
        from .reservation_rules import ReservationCheck

        return ReservationCheck(source=self, revision=revision)


@dataclass(frozen=True, slots=True, kw_only=True)
class SelectedAdmissionSource(SelectedSource):
    ingress_key: str
    admission_generation: int
    correction_witness: str
    input_digest: TextDigest
    original_digest: TextDigest

    def __post_init__(self):
        if not self.ingress_key or self.admission_generation <= 0 or not self.correction_witness:
            raise ValueError("Selected source requires its exact reserved input")

    @property
    def pending_input_key(self) -> str:
        return self.ingress_key

    def reservation_check(self, revision, inputs):
        from .reservation_rules import InputReservationCheck

        return InputReservationCheck(
            source=self, revision=revision, row=inputs.lookup(self.ingress_key)
        )

    def interrupted_check(self, revision, inputs, incarnation, turn):
        from .reservation_rules import InterruptedInputCheck

        return InterruptedInputCheck(
            source=self,
            revision=revision,
            row=inputs.lookup(self.ingress_key),
            incarnation=incarnation,
            turn=turn,
        )

    def matches_pending_input(self, key: str | None) -> bool:
        return self.ingress_key == key

    def original_has_started(self, inputs: InputDocument) -> bool:
        return inputs.lookup(self.ingress_key).proves_started(
            owner=self.incarnation,
            admission=self.admission_generation,
            turn=self.turn,
            sent_digest=self.input_digest,
            original_digest=self.original_digest,
        )
