"""Named ordinary-turn authority checks in the existing reservation rule family."""

from __future__ import annotations

from typing import TYPE_CHECKING


from .reservation_rules import ReservationRule, RuleCheck

if TYPE_CHECKING:
    pass


class MissingTurnOwnerCheck(RuleCheck):
    pass


class MissingTurnOwnerRule(ReservationRule):
    check_type = MissingTurnOwnerCheck
    explanation = "The ordinary turn's registered owner no longer exists."

    def violated(self, check: MissingTurnOwnerCheck) -> bool:
        return True


class MissingAcceptedInputCheck(RuleCheck):
    pass


class MissingAcceptedInputRule(ReservationRule):
    check_type = MissingAcceptedInputCheck
    explanation = "The accepted live input acknowledgement is missing."

    def violated(self, check: MissingAcceptedInputCheck) -> bool:
        return True


class UnboundOrdinaryInputCheck(RuleCheck):
    pass


class UnboundOrdinaryInputRule(ReservationRule):
    check_type = UnboundOrdinaryInputCheck
    explanation = "The durable input binding refused the exact native input."

    def violated(self, check: UnboundOrdinaryInputCheck) -> bool:
        return True


class UnconsumedDependencyCheck(RuleCheck):
    pass


class UnconsumedDependencyRule(ReservationRule):
    check_type = UnconsumedDependencyCheck
    explanation = "The dependency wait changed before its durable consumption."

    def violated(self, check: UnconsumedDependencyCheck) -> bool:
        return True
