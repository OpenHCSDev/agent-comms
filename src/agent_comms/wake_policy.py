"""Wake policy owns the initial disposition and engagement decision."""

from __future__ import annotations

from abc import abstractmethod
from dataclasses import dataclass
from typing import TYPE_CHECKING, ClassVar

from .coordination_errors import IntegrityViolationError
from .declared_family import DeclaredFamily
from .typed_table import sql_literal

if TYPE_CHECKING:
    from .historical_native_inputs import HistoricalNativeInput


@dataclass(frozen=True)
class Engagement(DeclaredFamily, affix="Engagement"):
    execution_id: str
    exact_target: str

    def __post_init__(self):
        if not self.execution_id or not self.exact_target:
            raise IntegrityViolationError("engaged claim requires execution and exact target")

    @property
    def mode(self) -> WakePolicy:
        return WakePolicy.decode(self.declared_name)()

    @property
    @abstractmethod
    def verdict(self) -> str | None: ...

    @classmethod
    def verdict_expression(cls, expression: str) -> str:
        cases = " ".join(
            f"WHEN {sql_literal(member.declared_name)} THEN {sql_literal(member.verdict)}"
            for member in cls.members_with(cls)
        )
        return f"CASE json_extract({expression}, '$.kind') {cases} END"


class FullEngagement(Engagement):
    verdict: ClassVar[str | None] = None


class BoundedTriageEngagement(Engagement):
    verdict: ClassVar[str | None] = "engage"


@dataclass(frozen=True)
class WakePolicy(DeclaredFamily, affix="Wake"):
    active: ClassVar[bool] = True
    triage: ClassVar[bool] = False

    @classmethod
    @abstractmethod
    def initial_state(cls): ...

    def engage(self, execution_id: str, target: str) -> Engagement:
        return Engagement.decode(self.declared_name)(execution_id, target)

    def triage_expectation(self):
        raise IntegrityViolationError("wake policy does not permit triage")

    @abstractmethod
    def proves_source(self, evidence: tuple[HistoricalNativeInput, ...]) -> bool: ...

    def full_expectation(self):
        return "this is yours; answer the original committed message"


class PassiveWake(WakePolicy):
    active = False

    def proves_source(self, evidence: tuple[HistoricalNativeInput, ...]) -> bool:
        return False

    @classmethod
    def initial_state(cls):
        from .assignment_states import PassiveAssignment

        return PassiveAssignment

    def engage(self, execution_id, target):
        raise IntegrityViolationError("passive claim cannot engage")


class BoundedTriageWake(WakePolicy):
    triage = True

    def proves_source(self, evidence: tuple[HistoricalNativeInput, ...]) -> bool:
        stages = {proof.stage: proof for proof in evidence}
        triage = stages.get("triage")
        if triage is None or not triage.expected_prompt_equality_established:
            return False
        if triage.triage_result == "ignore":
            return True
        return triage.triage_result == "full" and FullWake().proves_source(evidence)

    def triage_expectation(self):
        return "engage only if this concerns your assigned task; otherwise IGNORE"

    @classmethod
    def initial_state(cls):
        from .assignment_states import TriagePendingAssignment

        return TriagePendingAssignment


class FullWake(WakePolicy):
    def proves_source(self, evidence: tuple[HistoricalNativeInput, ...]) -> bool:
        return any(
            proof.stage == "full" and proof.expected_prompt_equality_established
            for proof in evidence
        )

    @classmethod
    def initial_state(cls):
        from .assignment_states import FullPendingAssignment

        return FullPendingAssignment
