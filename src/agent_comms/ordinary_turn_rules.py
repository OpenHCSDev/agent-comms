"""Named ordinary-turn authority checks in the existing reservation rule family."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from .reservation_rules import ReservationRule, RuleCheck
from .thread_identity import TurnId

if TYPE_CHECKING:
    from .comms import Comms
    from .goal_attempts import GoalAttemptStore, LaunchPermit
    from .goal_waits import GoalWait
    from .goals import GoalDeclaration
    from .input_drain import InputDrain
    from .queued_input import QueuedInput
    from .registry_document import RegistrySnapshot
    from .threads import Thread
    from .turn_input_source import TurnInputSource


class MissingTurnOwnerRules:
    """Shared applicability of MissingTurnOwnerCheck and its reservation rules."""

class OrdinaryOwnerRules:
    """Shared applicability of OrdinaryOwnerCheck and its reservation rules."""

class InputKeysRules:
    """Shared applicability of InputKeysCheck and its reservation rules."""

class MissingAcceptedInputRules:
    """Shared applicability of MissingAcceptedInputCheck and its reservation rules."""

class AcceptedInputRules:
    """Shared applicability of AcceptedInputCheck and its reservation rules."""

class OrdinaryContextRules:
    """Shared applicability of OrdinaryContextCheck and its reservation rules."""

class OrdinaryGoalGrantRules:
    """Shared applicability of OrdinaryGoalGrantCheck and its reservation rules."""

class UnboundOrdinaryInputRules:
    """Shared applicability of UnboundOrdinaryInputCheck and its reservation rules."""

class UnconsumedDependencyRules:
    """Shared applicability of UnconsumedDependencyCheck and its reservation rules."""


class MissingTurnOwnerCheck(MissingTurnOwnerRules, RuleCheck):
    pass


class MissingTurnOwnerRule(MissingTurnOwnerRules, ReservationRule):
    explanation = "The ordinary turn's registered owner no longer exists."

    def violated(self, check: MissingTurnOwnerCheck) -> bool:
        return True


@dataclass(frozen=True, kw_only=True)
class OrdinaryOwnerCheck(OrdinaryOwnerRules, RuleCheck):
    expected: Thread
    current: Thread
    registry: RegistrySnapshot
    canonical: str
    claim_generation: int
    turn: TurnId


class OrdinaryIncarnationRule(OrdinaryOwnerRules, ReservationRule):
    explanation = "The captured ordinary thread incarnation changed."

    def violated(self, check: OrdinaryOwnerCheck) -> bool:
        return not check.expected.incarnation.current(check.registry)


class OrdinaryRunningRule(OrdinaryOwnerRules, ReservationRule):
    explanation = "The ordinary owner is no longer running."

    def violated(self, check: OrdinaryOwnerCheck) -> bool:
        return not check.registry.status(check.canonical).running


class OrdinaryClaimRule(OrdinaryOwnerRules, ReservationRule):
    explanation = "The ordinary owner claim_scope generation changed."

    def violated(self, check: OrdinaryOwnerCheck) -> bool:
        return check.registry.claim_generation(check.canonical) != check.claim_generation


class OrdinaryProcessRule(OrdinaryOwnerRules, ReservationRule):
    explanation = "The ordinary turn belongs to another process incarnation."

    def violated(self, check: OrdinaryOwnerCheck) -> bool:
        return check.current.process_identity != check.expected.process_identity


class OrdinaryWorktreeRule(OrdinaryOwnerRules, ReservationRule):
    explanation = "The ordinary owner's worktree changed."

    def violated(self, check: OrdinaryOwnerCheck) -> bool:
        return check.current.worktree != check.expected.worktree


class OrdinaryTurnRule(OrdinaryOwnerRules, ReservationRule):
    explanation = "The captured ordinary turn is no longer the active turn."

    def violated(self, check: OrdinaryOwnerCheck) -> bool:
        return (
            not check.current.turn_record.read().matches(check.turn)
        )


@dataclass(frozen=True, kw_only=True)
class InputKeysCheck(InputKeysRules, RuleCheck):
    source: TurnInputSource
    text: str


class InputKeysRule(InputKeysRules, ReservationRule):
    explanation = "The input keys do not prove this exact original channel batch."

    def violated(self, check: InputKeysCheck) -> bool:
        return not check.source.valid_keys(check.text)


class MissingAcceptedInputCheck(MissingAcceptedInputRules, RuleCheck):
    pass


class MissingAcceptedInputRule(MissingAcceptedInputRules, ReservationRule):
    explanation = "The accepted live input acknowledgement is missing."

    def violated(self, check: MissingAcceptedInputCheck) -> bool:
        return True


@dataclass(frozen=True, kw_only=True)
class AcceptedInputCheck(AcceptedInputRules, RuleCheck):
    acknowledgement: QueuedInput
    current: Thread
    claim_generation: int
    input_id: str
    keys: tuple[str, ...]
    inputs: InputDrain
    session_id: str


class AcceptedInputAuthorityRule(AcceptedInputRules, ReservationRule):
    explanation = "The live input's accepted owner or goal authority changed."

    def violated(self, check: AcceptedInputCheck) -> bool:
        return not check.acknowledgement.current(check.current, check.claim_generation)


class AcceptedInputKeyRule(AcceptedInputRules, ReservationRule):
    explanation = "The input does not name its exact accepted ACP acknowledgement."

    def violated(self, check: AcceptedInputCheck) -> bool:
        return (check.input_id, check.keys) != (check.acknowledgement.input_id, (check.acknowledgement.key,))


class AcceptedInputSourceRule(AcceptedInputRules, ReservationRule):
    explanation = "The captured accepted input source is no longer live."

    def violated(self, check: AcceptedInputCheck) -> bool:
        return not check.acknowledgement.is_live(check.inputs, check.session_id)


@dataclass(frozen=True, kw_only=True)
class OrdinaryContextCheck(OrdinaryContextRules, RuleCheck):
    source: TurnInputSource
    owner: Thread
    goal: GoalDeclaration
    wait: GoalWait
    comms: Comms


class OrdinaryGoalRule(OrdinaryContextRules, ReservationRule):
    explanation = "The input's captured goal permission no longer applies."

    def violated(self, check: OrdinaryContextCheck) -> bool:
        return not check.source.goal_permission.allows(check.goal)


class OrdinaryDependencyRule(OrdinaryContextRules, ReservationRule):
    explanation = "The original dependency wait was removed or replaced."

    def violated(self, check: OrdinaryContextCheck) -> bool:
        return not check.source.dependency_current(check.wait)


class OrdinaryWaitRule(OrdinaryContextRules, ReservationRule):
    explanation = "The current dependency wait does not authorize this input."

    def violated(self, check: OrdinaryContextCheck) -> bool:
        return not check.source.allows_wait(
            check.wait, check.comms, check.owner.incarnation.require_recorded()
        )


class OrdinaryGoalInputRule(OrdinaryContextRules, ReservationRule):
    explanation = "An active goal does not authorize this routed input."

    def violated(self, check: OrdinaryContextCheck) -> bool:
        return not check.source.allows_goal_input(check.goal)


@dataclass(frozen=True, kw_only=True)
class OrdinaryGoalGrantCheck(OrdinaryGoalGrantRules, RuleCheck):
    permit: LaunchPermit
    store: GoalAttemptStore


class OrdinaryGoalGrantRule(OrdinaryGoalGrantRules, ReservationRule):
    explanation = "The captured autonomous goal launch is no longer claimed."

    def violated(self, check: OrdinaryGoalGrantCheck) -> bool:
        return not check.permit.is_claimed(check.store)


class UnboundOrdinaryInputCheck(UnboundOrdinaryInputRules, RuleCheck):
    pass


class UnboundOrdinaryInputRule(UnboundOrdinaryInputRules, ReservationRule):
    explanation = "The durable input binding refused the exact native input."

    def violated(self, check: UnboundOrdinaryInputCheck) -> bool:
        return True


class UnconsumedDependencyCheck(UnconsumedDependencyRules, RuleCheck):
    pass


class UnconsumedDependencyRule(UnconsumedDependencyRules, ReservationRule):
    explanation = "The dependency wait changed before its durable consumption."

    def violated(self, check: UnconsumedDependencyCheck) -> bool:
        return True
