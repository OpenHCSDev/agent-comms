"""Named start rules: each check's type selects the rules to apply."""

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

    @classmethod
    def rule_declarations(cls) -> tuple[type[ReservationRule], ...]:
        """Checks and their rules share capability parents; the registry owns rule order."""
        roots = (*RuleCheck.__mro__, *ReservationRule.__mro__)
        applicable = frozenset(
            rule
            for capability in cls.__mro__ if capability not in roots
            for rule in ReservationRule.members_with(capability)
        )
        return tuple(
            rule for rule in ReservationRule.members_with(ReservationRule)
            if rule in applicable
        )

    def require_valid(self) -> None:
        for declaration in self.rule_declarations():
            rule = declaration()
            if rule.violated(self):
                raise ReservationViolationError(rule)


class ReservationRule(DeclaredFamily, affix="Rule"):
    explanation: ClassVar[str]

    @abstractmethod
    def violated(self, check: RuleCheck) -> bool: ...
