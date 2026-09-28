"""Current selected-summary source records; journals reset at installation."""

from abc import abstractmethod
from dataclasses import dataclass
from typing import TYPE_CHECKING

from .child_process import ProcessIdentity
from .declared_family import DeclaredFamily
from .text_digest import TextDigest
from .thread_identity import ThreadIncarnation, TurnId

if TYPE_CHECKING:
    from .input_disposition import InputDocument
    from .reservation_rules import ReservationCheck

SessionRevision = tuple[tuple[int, int, int, int, int], tuple[int, int, int, int, int] | None]


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
        self, revision: SessionRevision | None, inputs: InputDocument
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
