"""Private send refusals discovered through the shared reservation rule family."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from .child_process import ProcessIdentity
from .coordination_tables.assignments import WakeAssignment
from .coordination_tables.participants import OwnerGenerations
from .reservation_rules import ReservationRule, RuleCheck
from .text_digest import TextDigest
from .threads import Thread
from .turn_goal_permission import GoalPermission

if TYPE_CHECKING:
    from .native_input_record import NativeInputRecord
    from .native_prompt_binding import PromptBinding
    from .native_runtime_input import NativeRuntimeInput
    from .private_send_stage import NativeSendStage


class RegistryIdentityRules:
    """Shared applicability of RegistryIdentityCheck and its reservation rules."""

class RegistryClaimRules:
    """Shared applicability of RegistryClaimCheck and its reservation rules."""

class GoalRegistryIdentityRules:
    """Shared applicability of GoalRegistryIdentityCheck and its reservation rules."""

class NativeIdentityRules:
    """Shared applicability of NativeIdentityCheck and its reservation rules."""

class NativeReservationRules:
    """Shared applicability of NativeReservationCheck and its reservation rules."""

class NativeBindingRules:
    """Shared applicability of NativeBindingCheck and its reservation rules."""

class NativeClaimRules:
    """Shared applicability of NativeClaimCheck and its reservation rules."""


@dataclass(frozen=True, kw_only=True)
class RegistryIdentityCheck(RegistryIdentityRules, RuleCheck):
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


class RegistryClaimCheck(RegistryClaimRules, RegistryIdentityCheck):
    """A claimed turn uses its captured settings, not the next-turn selection.

    Before a claim, configuration must still match. After a claim, the exact
    lease/process/session checks retain this operation's original authority;
    native launch and compaction use the captured Thread and observed model.
    """

    def model_matches(self) -> bool:
        return self.expected.turn_lease.leased or super().model_matches()

    def thinking_matches(self) -> bool:
        return self.expected.turn_lease.leased or super().thinking_matches()


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
        return super().worktree_matches() or (
            self.actual.project_resume_due
            and self.actual.turn_lease == self.expected.require_turn_lease()
            and self.actual.ran_in(self.expected.worktree)
        )


class GoalRegistryIdentityCheck(GoalRegistryIdentityRules, RegistryIdentityCheck):
    """Fresh turn claims capture the goal, not the earlier turn state."""


class GoalRegistryClaimCheck(GoalRegistryIdentityCheck, RegistryClaimCheck):
    """Selected input/awareness also captures the goal; reply settlement does not."""


class RegistryIncarnationRule(RegistryIdentityRules, ReservationRule):
    explanation = "The registered thread incarnation changed before native send."

    def violated(self, check: RegistryIdentityCheck) -> bool:
        return check.actual.incarnation != check.expected.incarnation


class RegistryNameRule(RegistryIdentityRules, ReservationRule):
    explanation = "The captured owner name changed."

    def violated(self, check: RegistryIdentityCheck) -> bool:
        return not check.name_matches()


class RegistryProcessRule(RegistryIdentityRules, ReservationRule):
    explanation = "The registered process is not the captured current owner process."

    def violated(self, check: RegistryIdentityCheck) -> bool:
        return (
            check.actual.process_identity != check.expected.process_identity
            or check.actual.process_identity != check.process
        )


class RegistryClaimRule(RegistryIdentityRules, ReservationRule):
    explanation = "The registry claim_scope generation changed."

    def violated(self, check: RegistryIdentityCheck) -> bool:
        return check.claim_generation != check.expected_claim


class RegistryRoleRule(RegistryIdentityRules, ReservationRule):
    explanation = "The captured owner role changed."

    def violated(self, check: RegistryIdentityCheck) -> bool:
        return check.actual.role != check.expected.role


class RegistryWorktreeRule(RegistryIdentityRules, ReservationRule):
    explanation = "The captured owner worktree changed."

    def violated(self, check: RegistryIdentityCheck) -> bool:
        return not check.worktree_matches()


class RegistryModelRule(RegistryIdentityRules, ReservationRule):
    explanation = "The captured owner's selected model changed."

    def violated(self, check: RegistryIdentityCheck) -> bool:
        return not check.model_matches()


class RegistryThinkingRule(RegistryIdentityRules, ReservationRule):
    explanation = "The captured owner's thinking level changed."

    def violated(self, check: RegistryIdentityCheck) -> bool:
        return not check.thinking_matches()


class RegistrySessionRule(RegistryIdentityRules, ReservationRule):
    explanation = "The captured owner's native session changed."

    def violated(self, check: RegistryIdentityCheck) -> bool:
        return check.actual.session_file != check.expected.session_file


class RegistryTurnRule(RegistryClaimRules, ReservationRule):
    explanation = "The exact captured active turn changed."

    def violated(self, check: RegistryClaimCheck) -> bool:
        return not check.actual.same_turn(check.expected)


class RegistryGoalRule(GoalRegistryIdentityRules, ReservationRule):
    explanation = "The captured goal changed."

    def violated(self, check: GoalRegistryIdentityCheck) -> bool:
        return not check.goal_permission.allows(check.actual.goal)


@dataclass(frozen=True, kw_only=True)
class NativeIdentityCheck(NativeIdentityRules, RuleCheck):
    row: NativeInputRecord
    stage: NativeSendStage
    owner: OwnerGenerations


@dataclass(frozen=True, kw_only=True)
class NativeReservationCheck(NativeReservationRules, NativeIdentityCheck):
    row: NativeRuntimeInput
    token_digest: str


@dataclass(frozen=True, kw_only=True)
class NativeBindingCheck(NativeBindingRules, NativeIdentityCheck):
    row: PromptBinding
    wire_root_id: str
    prompt_digest: TextDigest


class NativeInputIdentityRule(NativeIdentityRules, ReservationRule):
    explanation = "The original native input assignment, stage, attempt or owner changed."

    def violated(self, check: NativeIdentityCheck) -> bool:
        return check.row.identity != check.stage.identity(check.row.input_id, check.owner)


class NativeTokenRule(NativeReservationRules, ReservationRule):
    explanation = "The reserved native input token changed."

    def violated(self, check: NativeReservationCheck) -> bool:
        return check.row.owner_token_digest != check.token_digest


class NativeAlreadyBoundRule(NativeReservationRules, ReservationRule):
    explanation = "The native input was already bound to a sending claim_scope."

    def violated(self, check: NativeReservationCheck) -> bool:
        return check.row.sent_owner_claimed_generation.reservation_violation()


class NativeAlreadyProvenRule(NativeReservationRules, ReservationRule):
    explanation = "The native input already has a proven session result."

    def violated(self, check: NativeReservationCheck) -> bool:
        return check.row.reference.recorded


class NativeAlreadyDecidedRule(NativeReservationRules, ReservationRule):
    explanation = "The native triage input already has a verdict."

    def violated(self, check: NativeReservationCheck) -> bool:
        return check.row.verdict.decided


class NativeBindingRootRule(NativeBindingRules, ReservationRule):
    explanation = "The prelaunch binding belongs to a different wire root."

    def violated(self, check: NativeBindingCheck) -> bool:
        return check.row.wire_root_id != check.wire_root_id


class NativeBindingContentRule(NativeBindingRules, ReservationRule):
    explanation = "The native request digest differs from its durable prelaunch binding."

    def violated(self, check: NativeBindingCheck) -> bool:
        return TextDigest(check.row.expected_prompt_digest) != check.prompt_digest


@dataclass(frozen=True, kw_only=True)
class NativeClaimCheck(NativeClaimRules, RuleCheck):
    captured: WakeAssignment
    current: WakeAssignment


class NativeClaimRecipientRule(NativeClaimRules, ReservationRule):
    explanation = "The selected claim recipient changed."

    def violated(self, check: NativeClaimCheck) -> bool:
        return check.current.recipient != check.captured.recipient


class NativeClaimLookupRule(NativeClaimRules, ReservationRule):
    explanation = "The selected claim recipient lookup changed."

    def violated(self, check: NativeClaimCheck) -> bool:
        return check.current.recipient_lookup != check.captured.recipient_lookup


class NativeClaimSequenceRule(NativeClaimRules, ReservationRule):
    explanation = "The selected claim source sequence changed."

    def violated(self, check: NativeClaimCheck) -> bool:
        return check.current.wire_seq != check.captured.wire_seq


class NativeClaimMessageRule(NativeClaimRules, ReservationRule):
    explanation = "The selected claim committed message changed."

    def violated(self, check: NativeClaimCheck) -> bool:
        return check.current.message_id != check.captured.message_id


class NativeClaimModeRule(NativeClaimRules, ReservationRule):
    explanation = "The selected claim wake mode changed."

    def violated(self, check: NativeClaimCheck) -> bool:
        return check.current.lifecycle.mode != check.captured.lifecycle.mode
