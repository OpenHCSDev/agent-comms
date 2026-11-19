from __future__ import annotations

from abc import abstractmethod
from dataclasses import dataclass

from agent_comms.nominal.declared_family import DeclaredFamily


class FailureSource(DeclaredFamily, affix="FailureSource"):
    """Native message provenance, distinct from failures without a history source."""

    @property
    @abstractmethod
    def retained_in_history(self) -> bool:
        """Whether feedback belongs to native history, not a local failure notice.

        This declares ownership; it does not claim the journal append has finished.
        """

    def for_publication(self, text: str, sensitive: bool) -> FailureSource:
        return self

    def diagnostic_evidence(self) -> dict[str, object]:
        return {}

    def require_native(self) -> FailureSource:
        raise ValueError("Provider retry ended without its native failure message")


@dataclass(frozen=True)
class UnrecordedFailureSource(FailureSource):
    @property
    def retained_in_history(self) -> bool:
        return False
