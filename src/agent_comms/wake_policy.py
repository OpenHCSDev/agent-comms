"""Wake declarations own policy; state and engagement declarations bind its behavior."""

from __future__ import annotations

from abc import abstractmethod
from dataclasses import dataclass
from typing import TYPE_CHECKING, ClassVar

from .coordination_errors import IntegrityViolationError
from .declared_family import DeclaredFamily
from .source_proof_requirement import FullSourceProof, NoSourceProof, SourceProofRequirement, TriageSourceProof
from .typed_table import sql_literal

if TYPE_CHECKING:
    from .assignment_states import AssignmentState


@dataclass(frozen=True)
class WakePolicy(DeclaredFamily, SourceProofRequirement, affix="Wake"):
    initial_state: ClassVar[type[AssignmentState]]
    engagement_type: ClassVar[type[Engagement]]

    active: ClassVar[bool] = True
    triage: ClassVar[bool] = False

    def engage(self, execution_id: str, target: str) -> Engagement:
        return self.engagement_type(execution_id, target)

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

    def engage(self, execution_id: str, target: str) -> Engagement:
        raise IntegrityViolationError("passive claim cannot engage")


class BoundedTriageWake(TriageSourceProof, WakePolicy):
    triage = True

    def triage_expectation(self):
        return "evaluate this original under the shared reply relevance instruction"


class FullWake(FullSourceProof, WakePolicy):
    pass


@dataclass(frozen=True)
class Engagement(DeclaredFamily, affix="Engagement"):
    execution_id: str
    exact_target: str
    policy: ClassVar[type[WakePolicy]]

    def __init_subclass__(cls, **kwargs):
        super().__init_subclass__(**kwargs)
        cls.policy.engagement_type = cls

    def __post_init__(self):
        if not self.execution_id or not self.exact_target:
            raise IntegrityViolationError("engaged claim requires execution and exact target")

    @property
    def mode(self) -> WakePolicy:
        return self.policy()

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
    policy = FullWake
    verdict: ClassVar[str | None] = None


class BoundedTriageEngagement(Engagement):
    policy = BoundedTriageWake
    verdict: ClassVar[str | None] = "engage"
