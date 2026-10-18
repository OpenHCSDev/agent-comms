"""Wake policy owns the initial disposition and engagement decision."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import StrEnum
from typing import ClassVar

from .coordination_errors import IntegrityViolationError
from .declared_family import DeclaredFamily
from .source_proof_requirement import (
    FullSourceProof,
    NoSourceProof,
    SourceProofRequirement,
    TriageSourceProof,
)
from .typed_table import sql_literal


class MessageAudience(StrEnum):
    DIRECT = "direct"
    MENTIONED = "mentioned"
    COLLECTIVE = "collective"


class NoWakeReason(StrEnum):
    UNMENTIONED_OBSERVER = "unmentioned_observer"


@dataclass(frozen=True, slots=True)
class RecipientDecision(ABC):
    recipient: str  # Stable recipient_lookup, never the mutable canonical name.

    @abstractmethod
    def wake_policy(self) -> WakePolicy: ...

    @abstractmethod
    def claim_decisions(self) -> tuple[WakeDecision, ...]: ...

    @abstractmethod
    def candidate_wake_mode(self) -> type[WakePolicy] | None: ...

    @abstractmethod
    def to_wire(self) -> dict[str, str]: ...

    def initial_notification(self, recipient, sender_lookup, *, observation):
        return self.wake_policy().initial_state()().notification(
            recipient, sender_lookup, observation=observation)


@dataclass(frozen=True, slots=True)
class NoWakeDecision(RecipientDecision):
    """A frozen delivered member with no recipient-specific wake or claim."""

    reason: NoWakeReason = NoWakeReason.UNMENTIONED_OBSERVER

    def wake_policy(self) -> WakePolicy:
        return PassiveWake()

    def claim_decisions(self) -> tuple[WakeDecision, ...]:
        return ()

    def candidate_wake_mode(self) -> type[WakePolicy] | None:
        return None

    def to_wire(self) -> dict[str, str]:
        return {"recipient_lookup": self.recipient, "no_wake": self.reason.value}


@dataclass(frozen=True, slots=True)
class WakeDecision(RecipientDecision):
    audience: MessageAudience
    wake_mode: WakePolicy

    def wake_policy(self) -> WakePolicy:
        return self.wake_mode

    def claim_decisions(self) -> tuple[WakeDecision, ...]:
        return (self,)

    def candidate_wake_mode(self) -> type[WakePolicy]:
        return type(self.wake_mode)

    def to_wire(self) -> dict[str, str]:
        return {"recipient_lookup": self.recipient, "audience": self.audience.value,
                "wake_mode": self.wake_mode.declared_name}


@dataclass(frozen=True)
class Engagement(DeclaredFamily, affix="Engagement"):
    execution_id: str
    exact_target: str

    def __post_init__(self):
        if not self.execution_id or not self.exact_target:
            raise IntegrityViolationError("engaged claim requires execution and exact target")

    @property
    def mode(self) -> WakePolicy:
        return next(policy() for policy in WakePolicy.members_with(WakePolicy)
                    if policy.active and policy.engagement_type() is self.__class__)

    @property
    @abstractmethod
    def verdict(self) -> str | None: ...

    @classmethod
    def verdict_expression(cls, expression: str) -> str:
        cases = " ".join(
            f"WHEN {sql_literal(member)} THEN {sql_literal(member.verdict)}"
            for member in cls.members_with(cls)
        )
        return f"CASE json_extract({expression}, '$.kind') {cases} END"


class FullEngagement(Engagement):
    verdict: ClassVar[str | None] = None


class BoundedTriageEngagement(Engagement):
    verdict: ClassVar[str | None] = "engage"


@dataclass(frozen=True)
class WakePolicy(DeclaredFamily, SourceProofRequirement, affix="Wake"):
    active: ClassVar[bool] = True
    triage: ClassVar[bool] = False

    @classmethod
    @abstractmethod
    def initial_state(cls): ...

    @classmethod
    @abstractmethod
    def engagement_type(cls) -> type[Engagement]: ...

    def engage(self, execution_id: str, target: str) -> Engagement:
        return self.engagement_type()(execution_id, target)

    @classmethod
    def relevance_instruction(cls):
        from .turn_context import InstructionFile

        return InstructionFile.read("reply-relevance.md")

    def triage_expectation(self):
        raise IntegrityViolationError("wake policy does not permit triage")

    def full_expectation(self):
        return "this is yours; answer the original committed message"

    def recipient_decision(self, recipient: str, audience: MessageAudience):
        return WakeDecision(recipient, audience, self)


class PassiveWake(NoSourceProof, WakePolicy):
    active = False

    @classmethod
    def initial_state(cls):
        from .assignment_states import PassiveAssignment

        return PassiveAssignment

    @classmethod
    def engagement_type(cls) -> type[Engagement]:
        raise IntegrityViolationError("passive claim cannot engage")


class UnmentionedWake(PassiveWake):
    """A delivered observer, not even a passive claim."""

    def recipient_decision(self, recipient: str, audience: MessageAudience):
        return NoWakeDecision(recipient)


class BoundedTriageWake(TriageSourceProof, WakePolicy):
    triage = True

    @classmethod
    def engagement_type(cls) -> type[Engagement]:
        return BoundedTriageEngagement

    def triage_expectation(self):
        return "evaluate this original under the shared reply relevance instruction"

    @classmethod
    def initial_state(cls):
        from .assignment_states import TriagePendingAssignment

        return TriagePendingAssignment


class FullWake(FullSourceProof, WakePolicy):
    @classmethod
    def engagement_type(cls) -> type[Engagement]:
        return FullEngagement

    @classmethod
    def initial_state(cls):
        from .assignment_states import FullPendingAssignment

        return FullPendingAssignment
