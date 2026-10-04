"""Wake policy owns the initial disposition and engagement decision."""

from __future__ import annotations

from abc import abstractmethod
from dataclasses import dataclass
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
                    if policy.active and policy.engagement_type() is type(self))

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


class PassiveWake(NoSourceProof, WakePolicy):
    active = False

    @classmethod
    def initial_state(cls):
        from .assignment_states import PassiveAssignment

        return PassiveAssignment

    @classmethod
    def engagement_type(cls) -> type[Engagement]:
        raise IntegrityViolationError("passive claim cannot engage")


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
