"""Named reservation failures shared by reservation, recovery and commit."""

from __future__ import annotations

from abc import abstractmethod
from dataclasses import dataclass
from typing import ClassVar

from .child_process import ProcessIdentity
from .declared_family import DeclaredFamily
from .input_attempt import InputAttempt
from .selected_source import SelectedAdmissionSource, SelectedSource, SessionObservation
from .thread_identity import ThreadIncarnation, TurnId


class ReservationViolationError(ValueError):
    def __init__(self, rule: ReservationRule):
        self.rule = rule
        super().__init__(f"{rule.declared_name}: {rule.explanation}")


class RuleCheck:
    """A check selects applicable declarations from the one reservation rule family."""

    def require_valid(self) -> None:
        for declaration in ReservationRule.members_with(ReservationRule):
            if isinstance(self, declaration.check_type):
                rule = declaration()
                if rule.violated(self):
                    raise ReservationViolationError(rule)


@dataclass(frozen=True, kw_only=True)
class ReservationCheck(RuleCheck):
    source: SelectedSource
    revision: SessionObservation


@dataclass(frozen=True, kw_only=True)
class InputSourceCheck(ReservationCheck):
    source: SelectedAdmissionSource
    rows: tuple[InputAttempt, ...]


@dataclass(frozen=True, kw_only=True)
class InputReservationCheck(InputSourceCheck):
    """The original is still eligible for its first native binding."""


@dataclass(frozen=True, kw_only=True)
class OwnerReservationCheck(ReservationCheck):
    incarnation: ThreadIncarnation
    turn: TurnId


@dataclass(frozen=True, kw_only=True)
class CommitReservationCheck(OwnerReservationCheck):
    owner: ProcessIdentity
    pending_input_keys: tuple[str, ...]


@dataclass(frozen=True, kw_only=True)
class InterruptedReservationCheck(OwnerReservationCheck):
    """Same historical thread, earlier turn; never a grant to replay its input."""


@dataclass(frozen=True, kw_only=True)
class InterruptedInputCheck(InputSourceCheck, InterruptedReservationCheck):
    """Retirement checks an input observation, without making it sendable."""


class ReservationRule(DeclaredFamily, affix="Rule"):
    check_type: ClassVar[type[RuleCheck]] = ReservationCheck
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
        return not check.revision.matches(check.source.reserved_revision)


class IngressChangedRule(ReservationRule):
    check_type = CommitReservationCheck
    explanation = "The pending original input differs from the selected source."

    def violated(self, check: CommitReservationCheck) -> bool:
        return not check.source.matches_pending_inputs(check.pending_input_keys)


class MissingInputRule(ReservationRule):
    check_type = InputSourceCheck
    explanation = "The reserved original input no longer exists."

    def violated(self, check: InputSourceCheck) -> bool:
        return len(check.rows) != len(check.source.originals) or any(not row.exists for row in check.rows)


class AlreadySentRule(ReservationRule):
    check_type = InputReservationCheck
    explanation = "The original input is no longer reserved; it cannot authorize a send."

    def violated(self, check: InputSourceCheck) -> bool:
        return any(not row.accepts_reservation for row in check.rows)


class NativeBindingExistsRule(ReservationRule):
    check_type = InterruptedInputCheck
    explanation = "The original has a native binding; its outcome cannot be retired as unsent."

    def violated(self, check: InterruptedInputCheck) -> bool:
        return any(row.has_native_binding for row in check.rows)


class InputOwnerChangedRule(ReservationRule):
    check_type = InputSourceCheck
    explanation = "The reserved input belongs to a different owner."

    def violated(self, check: InputSourceCheck) -> bool:
        return any(not row.matches_owner(check.source.incarnation) for row in check.rows)


class AdmissionChangedRule(ReservationRule):
    check_type = InputSourceCheck
    explanation = "The reserved input belongs to a different admission generation."

    def violated(self, check: InputSourceCheck) -> bool:
        return any(not row.matches_admission(check.source.admission_generation) for row in check.rows)


class ContentChangedRule(ReservationRule):
    check_type = InputSourceCheck
    explanation = "The original input content changed after reservation."

    def violated(self, check: InputSourceCheck) -> bool:
        return any(
            not row.exists or row.key != original.key or row.digest != original.digest
            for original, row in zip(check.source.originals, check.rows, strict=True)
        )
