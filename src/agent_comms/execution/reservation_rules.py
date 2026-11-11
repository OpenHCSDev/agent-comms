"""Named start rules: each check's type selects the rules to apply."""

from __future__ import annotations

from abc import abstractmethod
from typing import ClassVar

from agent_comms.nominal.declared_family import DeclaredFamily


class ReservationViolationError(ValueError):
    def __init__(self, rule: ReservationRule, check: RuleCheck | None = None):
        self.rule = rule
        super().__init__(f"{rule.declared_name}: {rule.explanation_for(check)}")


class RuleCheck:
    """A check selects applicable declarations from the one reservation rule family."""

    def require_valid(self) -> None:
        for cls in type(self).__mro__:
            for declaration in cls.__dict__.get('_reservation_rules', ()):
                rule = declaration()
                if rule.violated(self):
                    raise ReservationViolationError(rule, self)


class ReservationRule(DeclaredFamily, affix="Rule"):
    check_type: ClassVar[type[RuleCheck]]
    explanation: ClassVar[str]

    def __init_subclass__(cls, **kwargs: object) -> None:
        super().__init_subclass__(**kwargs)
        if 'check_type' in vars(cls):
            own = cls.check_type.__dict__.get('_reservation_rules', ())
            cls.check_type._reservation_rules = own + (cls,)

    def explanation_for(self, check: RuleCheck | None) -> str:
        """The declared sentence, unless this rule reads the check to say what changed."""
        return self.explanation

    @abstractmethod
    def violated(self, check: RuleCheck) -> bool: ...
