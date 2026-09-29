"""Native stage requirements composed into the existing wake-policy declarations.

These capabilities have no names, registry, stored tags or policy discovery of
their own. Full evidence is shared by direct delivery and engaged triage through
inheritance; policy declarations retain lifecycle/engagement ownership.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .historical_native_inputs import HistoricalNativeInput


class SourceProofRequirement(ABC):
    @abstractmethod
    def proves_source(self, evidence: tuple[HistoricalNativeInput, ...]) -> bool: ...


class NoSourceProof(SourceProofRequirement):
    def proves_source(self, evidence: tuple[HistoricalNativeInput, ...]) -> bool:
        return False


class FullSourceProof(SourceProofRequirement):
    def proves_source(self, evidence: tuple[HistoricalNativeInput, ...]) -> bool:
        return any(
            proof.stage == "full" and proof.expected_prompt_equality_established
            for proof in evidence
        )


class TriageSourceProof(FullSourceProof):
    def proves_source(self, evidence: tuple[HistoricalNativeInput, ...]) -> bool:
        stages = {proof.stage: proof for proof in evidence}
        triage = stages.get("triage")
        if triage is None or not triage.expected_prompt_equality_established:
            return False
        if triage.triage_result == "ignore":
            return True
        return triage.triage_result == "full" and super().proves_source(evidence)
