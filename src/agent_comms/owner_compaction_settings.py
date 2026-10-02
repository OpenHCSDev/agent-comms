"""Actual selected-native policy evidence, decoded once at the RPC boundary.

The original native source budget decides preparation. These fields attest its
settings and decision; Python does not recompute a detached trigger.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from .message_reference import MessageReference
from .pi_vocabulary import CompactionReason



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
    task_aware: bool = field(metadata={"wire_name": "taskAware", "settings_exclude": True})
    reason: type[CompactionReason] = field(metadata={"settings_exclude": True})
    boundary: tuple[MessageReference, ...] = field(metadata={"settings_exclude": True})

    @property
    def trigger(self) -> bool:
        return self.reason.triggers

    def prepare(self, preparation):
        return self.reason.prepare(preparation)

    def require_prepared(self, result):
        self.reason.require_prepared(result)

    async def boundary_current(self, retained, owner, registry):
        return await self.reason.boundary_current(retained, self.boundary, owner, registry)

    def summary_settings(self) -> PiCompactionSettings:
        """Project this original decision into the existing native request type."""
        return PiCompactionSettings(self.reserve_tokens, self.keep_recent_tokens)

    def require_current(self, current: PiCompactionDecision) -> None:
        if current != self:
            raise PiSettingsEvidenceError("Selected native compaction settings changed")
