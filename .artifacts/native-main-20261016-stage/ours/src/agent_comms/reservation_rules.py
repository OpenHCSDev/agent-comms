"""Named admission rules: one rule family selected by each check's type."""

from __future__ import annotations

from abc import abstractmethod
from typing import ClassVar

from .declared_family import DeclaredFamily


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


class ReservationRule(DeclaredFamily, affix="Rule"):
    check_type: ClassVar[type[RuleCheck]]
    explanation: ClassVar[str]

    @abstractmethod
    def violated(self, check: RuleCheck) -> bool: ...
