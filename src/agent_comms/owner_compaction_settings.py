"""Read Pi's effective compaction settings without its mutable settings storage.

This is trigger evidence only, not owner authority, source capture, or a
provider request. The ACP owner binds the selected model/window and rechecks its source before
a native commit.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from pathlib import Path

from .native_package import verify_native_package
from .pi_helper import PiHelper


class PiSettingsEvidenceError(ValueError):
    """Effective Pi settings could not be read without mutation; skip trigger."""


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
    """A settings observation plus Pi's enablement and trigger decision."""

    enabled: bool = field(metadata={"settings_exclude": True})
    trigger: bool = field(metadata={"settings_exclude": True})

    def summary_settings(self) -> PiCompactionSettings:
        """Project this original decision into the existing native request type."""
        return PiCompactionSettings(self.reserve_tokens, self.keep_recent_tokens)

    def require_current(self, current: PiCompactionDecision) -> None:
        if current != self:
            raise PiSettingsEvidenceError("Selected native compaction settings changed")


@dataclass(frozen=True)
class CompactionDecisionRequest:
    package: str
    cwd: str
    context_tokens: int
    context_window: int

    def __post_init__(self):
        if not 0 <= self.context_tokens <= 2**53 - 1 or not 0 < self.context_window <= 2**53 - 1:
            raise PiSettingsEvidenceError("Invalid selected-model context evidence")


class CompactionDecisionHelper(PiHelper):
    script = Path(__file__).with_name("_pi_helpers") / "compaction_settings.mjs"
    request = CompactionDecisionRequest
    result = PiCompactionDecision


def read_compaction_decision(
    package: Path, worktree: str, *, context_tokens: int, context_window: int
) -> PiCompactionDecision:
    """Evaluate Pi's declared trigger on bounded evidence without mutating Pi.

    This returns no grant to generate a summary or write a session. The
    owner separately captures/rechecks turn, model and ingress.
    """
    try:
        package = package.resolve(strict=True)
        verify_native_package(package)
        cwd = Path(worktree).absolute()
        if cwd != cwd.resolve(strict=True) or not cwd.is_dir():
            raise PiSettingsEvidenceError("Worktree is not canonical")
        return asyncio.run(
            CompactionDecisionHelper.run(
                CompactionDecisionRequest(str(package), str(cwd), context_tokens, context_window),
                cwd=cwd,
            )
        )
    except (OSError, ValueError) as error:
        if isinstance(error, PiSettingsEvidenceError):
            raise
        raise PiSettingsEvidenceError("Pi settings decision unavailable") from error
