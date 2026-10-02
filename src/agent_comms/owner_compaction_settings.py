"""Actual selected-native policy evidence, decoded once at the RPC boundary.

The original native source budget decides preparation. These fields attest its
settings and decision; Python does not recompute a detached trigger.
"""

from __future__ import annotations

from dataclasses import dataclass, field



class PiSettingsEvidenceError(ValueError):
    """Actual selected policy evidence is unavailable; input stays unadmitted."""


@dataclass(frozen=True)
class PiCompactionSettings:
    """Pi's compaction budget, decoded by A2 once with application bounds."""

    reserve_tokens: int = field(metadata={"wire_name": "reserveTokens"})
    keep_recent_tokens: int = field(metadata={"wire_name": "keepRecentTokens"})

    def __post_init__(self):
        if (
            not 0 <= self.reserve_tokens <= 10_000_000
            or not 0 < self.keep_recent_tokens <= 10_000_000
        ):
            raise PiSettingsEvidenceError("Invalid effective Pi compaction settings")


@dataclass(frozen=True)
class PiCompactionDecision(PiCompactionSettings):
    """Selected policy result; trigger includes mandatory saved-context admission.

    enabled describes autonomous Pi compaction, not permission to ignore native
    preparation. The original native policy has already decided trigger.
    """

    enabled: bool = field(metadata={"settings_exclude": True})
    trigger: bool = field(metadata={"settings_exclude": True})

    def summary_settings(self) -> PiCompactionSettings:
        """Project this original decision into the existing native request type."""
        return PiCompactionSettings(self.reserve_tokens, self.keep_recent_tokens)

    def require_current(self, current: PiCompactionDecision) -> None:
        if current != self:
            raise PiSettingsEvidenceError("Selected native compaction settings changed")
