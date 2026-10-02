"""Current selected-summary source proofs; original journals require stopped carry."""

from __future__ import annotations

from abc import abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from .child_process import ProcessIdentity
from .coordination_errors import StaleRevision
from .declared_family import DeclaredFamily
from .private_path import FileRevision
from .input_origin import InputProvenance
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

    def matches_pending_inputs(self, keys: tuple[str, ...]) -> bool:
        return True

    def original_has_started(self, inputs: InputDocument) -> bool:
        return False

    @abstractmethod
    def summary_outcome(self, result, journal):
        """Bind the native result through this original source's admission contract."""

    @property
    @abstractmethod
    def pending_input_keys(self) -> tuple[str, ...]: ...

    @abstractmethod
    def reservation_check(
        self, revision: SessionObservation, inputs: InputDocument
    ) -> ReservationCheck: ...


@dataclass(frozen=True, slots=True, kw_only=True)
class ManualSource(SelectedSource):
    """Owner compaction without an InputDocument original-admission grant."""

    def summary_outcome(self, result, journal):
        return result.manual_summary(journal)

    @property
    def pending_input_keys(self) -> tuple[str, ...]:
        return ()

    def reservation_check(self, revision, inputs):
        from .reservation_rules import ReservationCheck

        return ReservationCheck(source=self, revision=revision)


@dataclass(frozen=True, slots=True, kw_only=True)
class SelectedAdmissionSource(SelectedSource):
    originals: tuple[InputProvenance, ...]
    admission_generation: int
    correction_witness: str
    input_digest: TextDigest

    @property
    def ingress_keys(self) -> tuple[str, ...]:
        return tuple(row.key for row in self.originals)

    @classmethod
    def capture(cls, owner, turn, admission, keys, inputs, text, revision):
        """Derive the whole original witness once from its actual input document."""
        rows = inputs.original_provenances(keys)
        if not rows:
            raise ValueError("Selected original input requires original receipts")
        digest = TextDigest.of(text)
        return cls(
            owner=owner.process_identity, incarnation=owner.incarnation,
            turn=turn, reserved_revision=revision, originals=rows,
            admission_generation=admission, correction_witness=f"{admission}:{digest.value}",
            input_digest=digest,
        )

    def __post_init__(self):
        if (
            not self.originals or any(not key for key in self.ingress_keys)
            or len(set(self.ingress_keys)) != len(self.ingress_keys)
            or self.admission_generation <= 0 or not self.correction_witness
        ):
            raise ValueError("Selected source requires its exact reserved input")

    def summary_outcome(self, result, journal):
        from .selected_summary_admission import SelectedAdmissionIdentity

        return result.adaptive_summary(
            journal, SelectedAdmissionIdentity(self, self.reserved_revision)
        )

    @property
    def pending_input_keys(self) -> tuple[str, ...]:
        return self.ingress_keys

    def reservation_check(self, revision, inputs):
        from .reservation_rules import InputReservationCheck

        return InputReservationCheck(
            source=self, revision=revision, rows=tuple(inputs.lookup(key) for key in self.ingress_keys)
        )

    def interrupted_check(self, revision, inputs, incarnation, turn):
        from .reservation_rules import InterruptedInputCheck

        return InterruptedInputCheck(
            source=self,
            revision=revision,
            rows=tuple(inputs.lookup(key) for key in self.ingress_keys),
            incarnation=incarnation,
            turn=turn,
        )

    def matches_pending_inputs(self, keys: tuple[str, ...]) -> bool:
        return self.ingress_keys == keys

    def original_has_started(self, inputs: InputDocument) -> bool:
        return all(inputs.lookup(original.key).proves_started(
            owner=self.incarnation,
            admission=self.admission_generation,
            turn=self.turn,
            sent_digest=self.input_digest,
            original_digest=original.digest,
        ) for original in self.originals)
