from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path

from agent_comms.team.rule_file import RuleFile


class DealtSources(ABC):
    """Which packaged files a session is dealt and which rules its audit answers. The deck
    readers (agent_comms_hooks.skill.DeckReader) are the one list, for every backend."""

    @classmethod
    @abstractmethod
    def rules(cls) -> tuple[RuleFile, ...]: ...

    @classmethod
    @abstractmethod
    def sources(cls) -> tuple[Path, ...]:
        """The packaged files ordered read in full: the rules themselves, never a copy."""
