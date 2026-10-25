"""Wake policy owns the initial disposition and engagement decision."""

from __future__ import annotations

from abc import abstractmethod
from dataclasses import dataclass
from enum import StrEnum
from typing import ClassVar

from agent_comms.field_codec import FieldCodec, ThreadLookup

from .coordination_errors import IntegrityViolationError
from .coordinator_vocabulary import CoordinatorVocabulary
from .declared_family import DeclaredFamily
from .lifecycle_capability import Active, LifecycleCapability
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
class RecipientDecision(DeclaredFamily):
    recipient: ThreadLookup  # Stable recipient_lookup, never the mutable canonical name.

    @abstractmethod
    def wake_policy(self) -> WakePolicy: ...

    @abstractmethod
    def claim_decisions(self) -> tuple[WakeDecision, ...]: ...

    @abstractmethod
    def candidate_wake_mode(self) -> type[WakePolicy] | None: ...

    @abstractmethod
    def to_wire(self) -> dict[str, str]: ...

    marker: ClassVar[str]  # the key only this decision's wire row carries
    @classmethod
    def from_wire(cls, row: dict[str, str]) -> RecipientDecision:
        """A committed row's decision, whose digest its first read proved."""
        member, = (member for member in cls.members_with(cls) if member.marker in row)
        return member.from_wire(row)

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

    marker = "no_wake"

    def to_wire(self) -> dict[str, str]:
        return {"recipient_lookup": self.recipient, "no_wake": self.reason.value}

    @classmethod
    def from_wire(cls, row: dict[str, str]) -> NoWakeDecision:
        match row:
            case {"recipient_lookup": lookup, "no_wake": reason}:
                return cls(FieldCodec.decode(ThreadLookup, lookup), NoWakeReason(reason))
        raise IntegrityViolationError("A no-wake decision row is incomplete.")


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

    marker = "wake_mode"

    def to_wire(self) -> dict[str, str]:
        return {"recipient_lookup": self.recipient, "audience": self.audience.value,
                "wake_mode": self.wake_mode.declared_name}

    @classmethod
    def from_wire(cls, row: dict[str, str]) -> WakeDecision:
        match row:
            case {"recipient_lookup": lookup, "audience": audience, "wake_mode": mode}:
                return cls(
                    FieldCodec.decode(ThreadLookup, lookup), MessageAudience(audience),
                    WakePolicy.member_named(mode)(),
                )
        raise IntegrityViolationError("A wake decision row is incomplete.")


@dataclass(frozen=True)
class Engagement(DeclaredFamily, affix="Engagement"):
    execution_id: str
    exact_target: str

    def __post_init__(self):
        if not self.execution_id or not self.exact_target:
            raise IntegrityViolationError("engaged claim requires execution and exact target")

    @property
    def mode(self) -> WakePolicy:
        return next(policy() for policy in WakePolicy.members_with(Active)
                    if policy.engagement_type() is self.__class__)

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
class WakePolicy(LifecycleCapability, CoordinatorVocabulary, SourceProofRequirement, affix="Wake"):
    triage: ClassVar[bool] = False

    @classmethod
    def sql_vocabulary(cls) -> dict[str, str]:
        return dict(wake_names=cls.sql_values(cls.names()))

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

    def recipient_decision(self, recipient: ThreadLookup, audience: MessageAudience):
        return WakeDecision(recipient, audience, self)


class PassiveWake(NoSourceProof, WakePolicy):

    @classmethod
    def initial_state(cls):
        from .assignment_states import PassiveAssignment

        return PassiveAssignment

    @classmethod
    def engagement_type(cls) -> type[Engagement]:
        raise IntegrityViolationError("passive claim cannot engage")


class UnmentionedWake(PassiveWake):
    """A delivered observer, not even a passive claim."""

    def recipient_decision(self, recipient: ThreadLookup, audience: MessageAudience):
        return NoWakeDecision(recipient)


class BoundedTriageWake(Active, TriageSourceProof, WakePolicy):
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


class FullWake(Active, FullSourceProof, WakePolicy):
    @classmethod
    def engagement_type(cls) -> type[Engagement]:
        return FullEngagement

    @classmethod
    def initial_state(cls):
        from .assignment_states import FullPendingAssignment

        return FullPendingAssignment
