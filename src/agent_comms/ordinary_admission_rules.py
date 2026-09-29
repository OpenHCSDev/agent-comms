"""Named ordinary-turn authority checks in the existing reservation rule family."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from .reservation_rules import ReservationRule, RuleCheck
from .thread_identity import TurnId

if TYPE_CHECKING:
    from .goal_attempts import GoalAttemptStore, LaunchPermit
    from .goal_waits import GoalWait
    from .goals import Goal
    from .queued_input import QueuedInput
    from .registry_document import RegistrySnapshot
    from .threads import Thread
    from .turn_input_binding import TurnInputBinding
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
    admission: int
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
        return not check.registry.statuses[check.canonical].running


class OrdinaryAdmissionRule(ReservationRule):
    check_type = OrdinaryOwnerCheck
    explanation = "The ordinary owner admission generation changed."

    def violated(self, check: OrdinaryOwnerCheck) -> bool:
        return check.registry.admission_generations.get(check.canonical) != check.admission


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
            check.current.active_turn is None or TurnId(check.current.active_turn.id) != check.turn
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
    explanation = "The accepted live input receipt is missing."

    def violated(self, check: MissingAcceptedInputCheck) -> bool:
        return True


@dataclass(frozen=True, kw_only=True)
class AcceptedInputCheck(RuleCheck):
    receipt: QueuedInput
    current: Thread
    admission: int
    wait: GoalWait | None
    input_id: str
    keys: tuple[str, ...]
    accepted_source: TurnInputSource | None


class AcceptedInputAuthorityRule(ReservationRule):
    check_type = AcceptedInputCheck
    explanation = "The live input's accepted owner, goal or wait authority changed."

    def violated(self, check: AcceptedInputCheck) -> bool:
        return not check.receipt.current(check.current, check.admission, check.wait)


class AcceptedInputKeyRule(ReservationRule):
    check_type = AcceptedInputCheck
    explanation = "The input does not name its exact accepted ACP receipt."

    def violated(self, check: AcceptedInputCheck) -> bool:
        return (check.input_id, check.keys) != (check.receipt.input_id, (check.receipt.key,))


class AcceptedInputSourceRule(ReservationRule):
    check_type = AcceptedInputCheck
    explanation = "The captured accepted input source is no longer live."

    def violated(self, check: AcceptedInputCheck) -> bool:
        return check.accepted_source != check.receipt.source()


@dataclass(frozen=True, kw_only=True)
class OrdinaryContextCheck(RuleCheck):
    source: TurnInputSource
    goal: Goal | None
    wait: GoalWait | None
    registry: RegistrySnapshot


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
        return not check.source.allows_wait(check.wait, check.registry)


class OrdinaryGoalInputRule(ReservationRule):
    check_type = OrdinaryContextCheck
    explanation = "An active goal does not authorize this routed input."

    def violated(self, check: OrdinaryContextCheck) -> bool:
        return not check.source.allows_goal_input(check.goal)


@dataclass(frozen=True, kw_only=True)
class OrdinaryJournalCheck(RuleCheck):
    binding: TurnInputBinding
    current: Thread


class OrdinaryJournalRule(ReservationRule):
    check_type = OrdinaryJournalCheck
    explanation = "The exact saved session has an unresolved compaction barrier."

    def violated(self, check: OrdinaryJournalCheck) -> bool:
        return not check.binding.available(check.current)


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
