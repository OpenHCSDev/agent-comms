"""Registry owner identity checks and reservation rules, independent of native input storage."""

from __future__ import annotations

from dataclasses import dataclass

from agent_comms.execution.child_process import ProcessIdentity
from agent_comms.execution.reservation_rules import ReservationRule, RuleCheck
from agent_comms.execution.turn_goal_permission import GoalPermission
from agent_comms.registry.threads import Thread


@dataclass(frozen=True, kw_only=True)
class RegistryIdentityCheck(RuleCheck):
    expected: Thread
    actual: Thread
    expected_claim: int
    claim_generation: int | None
    process: ProcessIdentity
    goal_permission: GoalPermission

    def name_matches(self) -> bool:
        return self.actual.name == self.expected.name

    def worktree_matches(self) -> bool:
        return self.actual.worktree == self.expected.worktree

    def model_matches(self) -> bool:
        return self.actual.model == self.expected.model

    def thinking_matches(self) -> bool:
        return self.actual.thinking_level == self.expected.thinking_level


class RegistryClaimCheck(RegistryIdentityCheck):
    """A claimed turn uses its captured settings, not the next-turn selection.

    Before a claim, configuration must still match. After a claim, the exact
    lease/process/session checks retain this operation's original authority;
    native launch and compaction use the captured Thread and observed model.
    """

    def model_matches(self) -> bool:
        return self.expected.turn_lease.is_leased() or super().model_matches()

    def thinking_matches(self) -> bool:
        return self.expected.turn_lease.is_leased() or super().thinking_matches()


class RunningTurnRegistryCheck(RegistryClaimCheck):
    """A turn that already started finishes in the project it started in.

    The registry's project selects the next turn, so changing it during a
    turn does not stop that turn from recording its result and replies. The
    same lease, process and session must remain, and the turn's directory
    must be one this conversation ran in. This accepts no new input.
    """

    def name_matches(self) -> bool:
        # RegistryOwner resolved the captured name through the registry's
        # retained alias. Recording this turn accepts no input under a new name.
        return True

    def worktree_matches(self) -> bool:
        return self.actual.continues_running_project(self.expected)


class GoalRegistryIdentityCheck(RegistryIdentityCheck):
    """Fresh turn claims capture the goal, not the earlier turn state."""


class GoalRegistryClaimCheck(GoalRegistryIdentityCheck, RegistryClaimCheck):
    """Selected input/awareness also captures the goal; reply settlement does not."""


class RegistryIncarnationRule(ReservationRule):
    check_type = RegistryIdentityCheck
    explanation = "The registered thread incarnation changed before native send."

    def violated(self, check: RegistryIdentityCheck) -> bool:
        return check.actual.incarnation != check.expected.incarnation


class RegistryNameRule(ReservationRule):
    check_type = RegistryIdentityCheck
    explanation = "The captured owner name changed."

    def violated(self, check: RegistryIdentityCheck) -> bool:
        return not check.name_matches()


class RegistryProcessRule(ReservationRule):
    check_type = RegistryIdentityCheck
    explanation = "The registered process is not the captured current owner process."

    def violated(self, check: RegistryIdentityCheck) -> bool:
        return (
            check.actual.process_identity != check.expected.process_identity
            or check.actual.process_identity != check.process
        )


class RegistryClaimRule(ReservationRule):
    check_type = RegistryIdentityCheck
    explanation = "The registry claim_scope generation changed."

    def violated(self, check: RegistryIdentityCheck) -> bool:
        return check.claim_generation != check.expected_claim


class RegistryRoleRule(ReservationRule):
    check_type = RegistryIdentityCheck
    explanation = "The captured owner role changed."

    def violated(self, check: RegistryIdentityCheck) -> bool:
        return check.actual.role != check.expected.role


class RegistryWorktreeRule(ReservationRule):
    check_type = RegistryIdentityCheck
    explanation = "The captured owner worktree changed."

    def violated(self, check: RegistryIdentityCheck) -> bool:
        return not check.worktree_matches()


class RegistryModelRule(ReservationRule):
    check_type = RegistryIdentityCheck
    explanation = "The captured owner's selected model changed."

    def violated(self, check: RegistryIdentityCheck) -> bool:
        return not check.model_matches()


class RegistryThinkingRule(ReservationRule):
    check_type = RegistryIdentityCheck
    explanation = "The captured owner's thinking level changed."

    def violated(self, check: RegistryIdentityCheck) -> bool:
        return not check.thinking_matches()


class RegistrySessionRule(ReservationRule):
    check_type = RegistryIdentityCheck
    explanation = "The captured owner's native session changed."

    def violated(self, check: RegistryIdentityCheck) -> bool:
        return check.actual.session_file != check.expected.session_file


class RegistryTurnRule(ReservationRule):
    check_type = RegistryClaimCheck
    explanation = "The exact captured active turn changed."

    def violated(self, check: RegistryClaimCheck) -> bool:
        return not check.actual.same_turn(check.expected)


class RegistryGoalRule(ReservationRule):
    check_type = GoalRegistryIdentityCheck
    explanation = "The captured goal changed."

    def violated(self, check: GoalRegistryIdentityCheck) -> bool:
        return not check.goal_permission.allows(check.actual.goal)
