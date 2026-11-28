"""Private send refusals discovered through the shared reservation rule family."""

from __future__ import annotations

from dataclasses import dataclass

from agent_comms.coordination.tables.assignments import WakeAssignment
from agent_comms.execution.native_input_record import NativeInputIdentity, NativeInputRecord
from agent_comms.execution.native_runtime_input import NativeRuntimeInput
from agent_comms.execution.reservation_rules import ReservationRule, RuleCheck


@dataclass(frozen=True, kw_only=True)
class NativeIdentityCheck(RuleCheck):
    row: NativeInputRecord
    expected: NativeInputIdentity


@dataclass(frozen=True, kw_only=True)
class NativeReservationCheck(NativeIdentityCheck):
    row: NativeRuntimeInput
    token_digest: str


class NativeInputIdentityRule(ReservationRule):
    check_type = NativeIdentityCheck
    explanation = "The original native input assignment, stage, attempt or owner changed."

    def violated(self, check: NativeIdentityCheck) -> bool:
        return check.row.identity != check.expected


class NativeTokenRule(ReservationRule):
    check_type = NativeReservationCheck
    explanation = "The reserved native input token changed."

    def violated(self, check: NativeReservationCheck) -> bool:
        return check.row.owner_token_digest != check.token_digest


class NativeAlreadyBoundRule(ReservationRule):
    check_type = NativeReservationCheck
    explanation = "The native input was already bound to a sending claim_scope."

    def violated(self, check: NativeReservationCheck) -> bool:
        return check.row.sent_owner_claimed_generation.reservation_violation()


class NativeAlreadyProvenRule(ReservationRule):
    check_type = NativeReservationCheck
    explanation = "The native input already has a proven session result."

    def violated(self, check: NativeReservationCheck) -> bool:
        return check.row.reference.has_session_proof()


class NativeAlreadyDecidedRule(ReservationRule):
    check_type = NativeReservationCheck
    explanation = "The native triage input already has a verdict."

    def violated(self, check: NativeReservationCheck) -> bool:
        return check.row.verdict.is_decided()


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
