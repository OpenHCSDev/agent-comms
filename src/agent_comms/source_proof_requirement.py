"""Native stage requirements composed into the existing wake-policy declarations.

These capabilities have no names, registry, stored tags or policy discovery of
their own. Full evidence is shared by direct delivery and engaged triage through
inheritance; policy declarations retain lifecycle/engagement ownership.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .historical_native_inputs import HistoricalNativeInput


class SourceProofRequirement(ABC):
    @abstractmethod
    def proves_source(self, evidence: tuple[HistoricalNativeInput, ...]) -> bool: ...


class SourceSettlement(ABC):
    """Whether a claim is settled, separately from whether it injected native input."""

    @abstractmethod
    def apply(self, inputs: list[HistoricalNativeInput]) -> bool: ...


@dataclass(frozen=True)
class ProvedByInput(SourceSettlement):
    evidence: tuple[HistoricalNativeInput, ...]

    def apply(self, inputs: list[HistoricalNativeInput]) -> bool:
        inputs.extend(self.evidence)
        return True


class SettledWithoutInput(SourceSettlement):
    def apply(self, inputs: list[HistoricalNativeInput]) -> bool:
        return True


class Unproved(SourceSettlement):
    def apply(self, inputs: list[HistoricalNativeInput]) -> bool:
        return False


class NoSourceProof(SourceProofRequirement):
    def proves_source(self, evidence: tuple[HistoricalNativeInput, ...]) -> bool:
        return False


class FullSourceProof(SourceProofRequirement):
    def proves_source(self, evidence: tuple[HistoricalNativeInput, ...]) -> bool:
        return any(
            proof.execution.proves_full_source(proof)
            for proof in evidence
        )


class TriageSourceProof(FullSourceProof):
    def proves_source(self, evidence: tuple[HistoricalNativeInput, ...]) -> bool:
        # A mandatory full input can consider pending triage peers in the same
        # batch. Its original bound source proof is stronger than a triage-only
        # verdict; never manufacture a second triage input to cover it.
        return super().proves_source(evidence) or any(
            proof.proves_triage_source(evidence)
            for proof in evidence
        )
