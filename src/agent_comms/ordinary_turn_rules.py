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


class MissingTurnOwnerCheck(RuleCheck):
    pass


class MissingTurnOwnerRule(ReservationRule):
    check_type = MissingTurnOwnerCheck
    explanation = "The ordinary turn's registered owner no longer exists."

    def violated(self, check: MissingTurnOwnerCheck) -> bool:
        return True


@dataclass(frozen=True, kw_only=True)
class OrdinaryOwnerCheck(RuleCheck):
    expected: Thread
    current: Thread
    registry: RegistrySnapshot
    canonical: str
    claim_scope: int
    turn: TurnId


class OrdinaryIncarnationRule(ReservationRule):
    check_type = OrdinaryOwnerCheck
    explanation = "The captured ordinary thread incarnation changed."

    def violated(self, check: OrdinaryOwnerCheck) -> bool:
        return not check.expected.incarnation.current(check.registry)


class OrdinaryRunningRule(ReservationRule):
    check_type = OrdinaryOwnerCheck
    explanation = "The ordinary owner is no longer running."

    def violated(self, check: OrdinaryOwnerCheck) -> bool:
        return not check.registry.status(check.canonical).running


class OrdinaryClaimRule(ReservationRule):
    check_type = OrdinaryOwnerCheck
    explanation = "The ordinary owner claim_scope generation changed."

    def violated(self, check: OrdinaryOwnerCheck) -> bool:
        return check.registry.claim_generation(check.canonical) != check.claim_scope


class OrdinaryProcessRule(ReservationRule):
    check_type = OrdinaryOwnerCheck
    explanation = "The ordinary turn belongs to another process incarnation."

    def violated(self, check: OrdinaryOwnerCheck) -> bool:
        return check.current.process_identity != check.expected.process_identity


class OrdinaryWorktreeRule(ReservationRule):
    check_type = OrdinaryOwnerCheck
    explanation = "The ordinary owner's worktree changed."

    def violated(self, check: OrdinaryOwnerCheck) -> bool:
        return check.current.worktree != check.expected.worktree


class OrdinaryTurnRule(ReservationRule):
    check_type = OrdinaryOwnerCheck
    explanation = "The captured ordinary turn is no longer the active turn."

    def violated(self, check: OrdinaryOwnerCheck) -> bool:
        return (
            not check.current.turn_record.read().matches(check.turn)
        )


@dataclass(frozen=True, kw_only=True)
class InputKeysCheck(RuleCheck):
    source: TurnInputSource
    text: str


class InputKeysRule(ReservationRule):
    check_type = InputKeysCheck
    explanation = "The input keys do not prove this exact original channel batch."

    def violated(self, check: InputKeysCheck) -> bool:
        return not check.source.valid_keys(check.text)


class MissingAcceptedInputCheck(RuleCheck):
    pass


class MissingAcceptedInputRule(ReservationRule):
    check_type = MissingAcceptedInputCheck
    explanation = "The accepted live input acknowledgement is missing."

    def violated(self, check: MissingAcceptedInputCheck) -> bool:
        return True


@dataclass(frozen=True, kw_only=True)
class AcceptedInputCheck(RuleCheck):
    acknowledgement: QueuedInput
    current: Thread
    claim_scope: int
    input_id: str
    keys: tuple[str, ...]
    inputs: InputDrain
    session_id: str


class AcceptedInputAuthorityRule(ReservationRule):
    check_type = AcceptedInputCheck
    explanation = "The live input's accepted owner or goal authority changed."

    def violated(self, check: AcceptedInputCheck) -> bool:
        return not check.acknowledgement.current(check.current, check.claim_scope)


class AcceptedInputKeyRule(ReservationRule):
    check_type = AcceptedInputCheck
    explanation = "The input does not name its exact accepted ACP acknowledgement."

    def violated(self, check: AcceptedInputCheck) -> bool:
        return (check.input_id, check.keys) != (check.acknowledgement.input_id, (check.acknowledgement.key,))


class AcceptedInputSourceRule(ReservationRule):
    check_type = AcceptedInputCheck
    explanation = "The captured accepted input source is no longer live."

    def violated(self, check: AcceptedInputCheck) -> bool:
        return not check.acknowledgement.is_live(check.inputs, check.session_id)


@dataclass(frozen=True, kw_only=True)
class OrdinaryContextCheck(RuleCheck):
    source: TurnInputSource
    owner: Thread
    goal: GoalDeclaration
    wait: GoalWait
    comms: Comms


class OrdinaryGoalRule(ReservationRule):
    check_type = OrdinaryContextCheck
    explanation = "The input's captured goal permission no longer applies."

    def violated(self, check: OrdinaryContextCheck) -> bool:
        return not check.source.goal_permission.allows(check.goal)


class OrdinaryDependencyRule(ReservationRule):
    check_type = OrdinaryContextCheck
    explanation = "The original dependency wait was removed or replaced."

    def violated(self, check: OrdinaryContextCheck) -> bool:
        return not check.source.dependency_current(check.wait)


class OrdinaryWaitRule(ReservationRule):
    check_type = OrdinaryContextCheck
    explanation = "The current dependency wait does not authorize this input."

    def violated(self, check: OrdinaryContextCheck) -> bool:
        return not check.source.allows_wait(
            check.wait, check.comms, check.owner.incarnation.require_recorded()
        )


class OrdinaryGoalInputRule(ReservationRule):
    check_type = OrdinaryContextCheck
    explanation = "An active goal does not authorize this routed input."

    def violated(self, check: OrdinaryContextCheck) -> bool:
        return not check.source.allows_goal_input(check.goal)


@dataclass(frozen=True, kw_only=True)
class OrdinaryGoalGrantCheck(RuleCheck):
    permit: LaunchPermit
    store: GoalAttemptStore


class OrdinaryGoalGrantRule(ReservationRule):
    check_type = OrdinaryGoalGrantCheck
    explanation = "The captured autonomous goal launch is no longer claimed."

    def violated(self, check: OrdinaryGoalGrantCheck) -> bool:
        return not check.permit.is_claimed(check.store)


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
