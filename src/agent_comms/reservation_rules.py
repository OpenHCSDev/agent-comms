"""Named reservation failures shared by reservation, recovery and commit."""

from __future__ import annotations

from abc import abstractmethod
from dataclasses import dataclass
from typing import ClassVar

from .child_process import ProcessIdentity
from .declared_family import DeclaredFamily
from .input_attempt import InputAttempt
from .selected_source import SelectedAdmissionSource, SelectedSource, SessionRevision
from .thread_identity import ThreadIncarnation, TurnId


class ReservationViolationError(ValueError):
    def __init__(self, rule: ReservationRule):
        self.rule = rule
        super().__init__(f"{rule.declared_name}: {rule.explanation}")


@dataclass(frozen=True, kw_only=True)
class ReservationCheck:
    source: SelectedSource
    revision: SessionRevision | None

    def require_valid(self) -> None:
        for declaration in ReservationRule.members_with(ReservationRule):
            if isinstance(self, declaration.check_type):
                rule = declaration()
                if rule.violated(self):
                    raise ReservationViolationError(rule)


@dataclass(frozen=True, kw_only=True)
class InputReservationCheck(ReservationCheck):
    source: SelectedAdmissionSource
    row: InputAttempt


@dataclass(frozen=True, kw_only=True)
class OwnerReservationCheck(ReservationCheck):
    incarnation: ThreadIncarnation
    turn: TurnId


@dataclass(frozen=True, kw_only=True)
class CommitReservationCheck(OwnerReservationCheck):
    owner: ProcessIdentity
    pending_input_key: str | None


@dataclass(frozen=True, kw_only=True)
class InterruptedReservationCheck(InputReservationCheck, OwnerReservationCheck):
    """Same historical thread, earlier turn; never a grant to replay its input."""


class ReservationRule(DeclaredFamily, affix="Rule"):
    check_type: ClassVar[type[ReservationCheck]] = ReservationCheck
    explanation: ClassVar[str]

    @abstractmethod
    def violated(self, check: ReservationCheck) -> bool: ...


class OwnerChangedRule(ReservationRule):
    check_type = OwnerReservationCheck
    explanation = "The selected source belongs to a different thread incarnation."

    def violated(self, check: OwnerReservationCheck) -> bool:
        return check.source.incarnation != check.incarnation


class ProcessChangedRule(ReservationRule):
    check_type = CommitReservationCheck
    explanation = "The selected source belongs to a different owner process."

    def violated(self, check: CommitReservationCheck) -> bool:
        return check.source.owner != check.owner


class TurnChangedRule(ReservationRule):
    check_type = CommitReservationCheck
    explanation = "The selected source belongs to a different turn."

    def violated(self, check: CommitReservationCheck) -> bool:
        return check.source.turn != check.turn


class TurnStillActiveRule(ReservationRule):
    check_type = InterruptedReservationCheck
    explanation = "The interrupted summary belongs to the current turn."

    def violated(self, check: InterruptedReservationCheck) -> bool:
        return check.source.turn == check.turn


class SessionChangedRule(ReservationRule):
    explanation = "The saved session or input proof changed after reservation."

    def violated(self, check: ReservationCheck) -> bool:
        return check.source.reserved_revision != check.revision


class IngressChangedRule(ReservationRule):
    check_type = CommitReservationCheck
    explanation = "The pending original input differs from the selected source."

    def violated(self, check: CommitReservationCheck) -> bool:
        return not check.source.matches_pending_input(check.pending_input_key)


class MissingInputRule(ReservationRule):
    check_type = InputReservationCheck
    explanation = "The reserved original input no longer exists."

    def violated(self, check: InputReservationCheck) -> bool:
        return not check.row.exists


class AlreadySentRule(ReservationRule):
    check_type = InputReservationCheck
    explanation = "The original input is no longer reserved; it cannot authorize a send."

    def violated(self, check: InputReservationCheck) -> bool:
        return not check.row.accepts_reservation


class InputOwnerChangedRule(ReservationRule):
    check_type = InputReservationCheck
    explanation = "The reserved input belongs to a different owner."

    def violated(self, check: InputReservationCheck) -> bool:
        return not check.row.matches_owner(check.source.incarnation)


class AdmissionChangedRule(ReservationRule):
    check_type = InputReservationCheck
    explanation = "The reserved input belongs to a different admission generation."

    def violated(self, check: InputReservationCheck) -> bool:
        return not check.row.matches_admission(check.source.admission_generation)


class ContentChangedRule(ReservationRule):
    check_type = InputReservationCheck
    explanation = "The original input content changed after reservation."

    def violated(self, check: InputReservationCheck) -> bool:
        return check.row.digest != check.source.original_digest
