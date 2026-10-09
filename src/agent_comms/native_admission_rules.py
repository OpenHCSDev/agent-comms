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

if TYPE_CHECKING:
    from .native_input_record import NativeInputRecord
    from .native_prompt_binding import PromptBinding
    from .native_runtime_input import NativeRuntimeInput
    from .private_send_stage import NativeSendStage


@dataclass(frozen=True, kw_only=True)
class RegistryIdentityCheck(RuleCheck):
    expected: Thread
    actual: Thread
    expected_admission: int
    admission: int | None
    process: ProcessIdentity

    def worktree_matches(self) -> bool:
        return self.actual.worktree == self.expected.worktree

    def model_matches(self) -> bool:
        return self.actual.model == self.expected.model

    def thinking_matches(self) -> bool:
        return self.actual.thinking_level == self.expected.thinking_level


class RegistryAdmissionCheck(RegistryIdentityCheck):
    """A claimed turn uses its captured settings, not the next-turn selection.

    Before a claim, configuration must still match. After a claim, the exact
    lease/process/session checks retain this operation's original authority;
    native launch and compaction use the captured Thread and observed model.
    """

    def model_matches(self) -> bool:
        return self.expected.turn_lease is not None or super().model_matches()

    def thinking_matches(self) -> bool:
        return self.expected.turn_lease is not None or super().thinking_matches()


class RegistryPublicationCheck(RegistryAdmissionCheck):
    """Publish this admitted source after next-turn project/settings changes.

    This grants no input admission. The same lease/process/source must remain;
    the captured executing directory must still belong to this conversation.
    """

    def worktree_matches(self) -> bool:
        return self.actual.contains_worktree(self.expected.worktree)


class GoalRegistryIdentityCheck(RegistryIdentityCheck):
    """Fresh turn claims capture the goal, not the earlier turn state."""


class GoalRegistryAdmissionCheck(GoalRegistryIdentityCheck, RegistryAdmissionCheck):
    """Selected input/awareness also captures the goal; reply settlement does not."""


class RegistryIncarnationRule(ReservationRule):
    check_type = RegistryIdentityCheck
    explanation = "The registered thread incarnation changed before native send."

    def violated(self, check: RegistryIdentityCheck) -> bool:
        return check.actual.incarnation != check.expected.incarnation


class RegistryProcessRule(ReservationRule):
    check_type = RegistryIdentityCheck
    explanation = "The registered process is not the captured current owner process."

    def violated(self, check: RegistryIdentityCheck) -> bool:
        return (
            check.actual.process_identity != check.expected.process_identity
            or check.actual.process_identity != check.process
        )


class RegistryAdmissionRule(ReservationRule):
    check_type = RegistryIdentityCheck
    explanation = "The registry admission generation changed."

    def violated(self, check: RegistryIdentityCheck) -> bool:
        return check.admission != check.expected_admission


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
    check_type = RegistryAdmissionCheck
    explanation = "The exact captured active turn changed."

    def violated(self, check: RegistryAdmissionCheck) -> bool:
        return check.actual.turn_lease != check.expected.turn_lease


class RegistryGoalRule(ReservationRule):
    check_type = GoalRegistryIdentityCheck
    explanation = "The captured goal changed."

    def violated(self, check: GoalRegistryIdentityCheck) -> bool:
        return check.actual.goal != check.expected.goal


@dataclass(frozen=True, kw_only=True)
class NativeIdentityCheck(RuleCheck):
    row: NativeInputRecord
    stage: NativeSendStage
    owner: OwnerGenerations


@dataclass(frozen=True, kw_only=True)
class NativeReservationCheck(NativeIdentityCheck):
    row: NativeRuntimeInput
    token_digest: str


@dataclass(frozen=True, kw_only=True)
class NativeBindingCheck(NativeIdentityCheck):
    row: PromptBinding
    wire_root_id: str
    prompt_digest: TextDigest


class NativeInputIdentityRule(ReservationRule):
    check_type = NativeIdentityCheck
    explanation = "The original native input assignment, stage, attempt or owner changed."

    def violated(self, check: NativeIdentityCheck) -> bool:
        return check.row.identity != check.stage.identity(check.row.input_id, check.owner)


class NativeTokenRule(ReservationRule):
    check_type = NativeReservationCheck
    explanation = "The reserved native input token changed."

    def violated(self, check: NativeReservationCheck) -> bool:
        return check.row.owner_token_digest != check.token_digest


class NativeAlreadyAdmittedRule(ReservationRule):
    check_type = NativeReservationCheck
    explanation = "The native input was already bound to a sending admission."

    def violated(self, check: NativeReservationCheck) -> bool:
        return check.row.sent_owner_admission_generation.reservation_violation()


class NativeAlreadyProvenRule(ReservationRule):
    check_type = NativeReservationCheck
    explanation = "The native input already has a proven session result."

    def violated(self, check: NativeReservationCheck) -> bool:
        return check.row.reference.recorded


class NativeAlreadyDecidedRule(ReservationRule):
    check_type = NativeReservationCheck
    explanation = "The native triage input already has a verdict."

    def violated(self, check: NativeReservationCheck) -> bool:
        return check.row.verdict is not None


class NativeBindingRootRule(ReservationRule):
    check_type = NativeBindingCheck
    explanation = "The prelaunch binding belongs to a different wire root."

    def violated(self, check: NativeBindingCheck) -> bool:
        return check.row.wire_root_id != check.wire_root_id


class NativeBindingContentRule(ReservationRule):
    check_type = NativeBindingCheck
    explanation = "The native request digest differs from its durable prelaunch binding."

    def violated(self, check: NativeBindingCheck) -> bool:
        return TextDigest(check.row.expected_prompt_digest) != check.prompt_digest


@dataclass(frozen=True, kw_only=True)
class NativeClaimCheck(RuleCheck):
    captured: WakeAssignment
    current: WakeAssignment


class NativeClaimRecipientRule(ReservationRule):
    check_type = NativeClaimCheck
    explanation = "The selected claim recipient changed."

    def violated(self, check: NativeClaimCheck) -> bool:
        return check.current.recipient != check.captured.recipient


class NativeClaimLookupRule(ReservationRule):
    check_type = NativeClaimCheck
    explanation = "The selected claim recipient lookup changed."

    def violated(self, check: NativeClaimCheck) -> bool:
        return check.current.recipient_lookup != check.captured.recipient_lookup


class NativeClaimSequenceRule(ReservationRule):
    check_type = NativeClaimCheck
    explanation = "The selected claim source sequence changed."

    def violated(self, check: NativeClaimCheck) -> bool:
        return check.current.wire_seq != check.captured.wire_seq


class NativeClaimMessageRule(ReservationRule):
    check_type = NativeClaimCheck
    explanation = "The selected claim committed message changed."

    def violated(self, check: NativeClaimCheck) -> bool:
        return check.current.message_id != check.captured.message_id


class NativeClaimModeRule(ReservationRule):
    check_type = NativeClaimCheck
    explanation = "The selected claim wake mode changed."

    def violated(self, check: NativeClaimCheck) -> bool:
        return check.current.lifecycle.mode != check.captured.lifecycle.mode
