"""Original saved-conversation values shared by import and native publication."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path

from agent_comms.execution.turn_context import HistoricalInstructionProvenance
from agent_comms.installer.harness_install import Harness
from agent_comms.registry.thread_text import NoThreadText, ThreadText


@dataclass(frozen=True, slots=True, kw_only=True)
class ImportedConversation(ABC):
    """A session imported as a stopped thread: where it ran and the Pi session it becomes."""

    harness: type[Harness]
    source_id: str
    project: str
    messages_seen: int
    title: ThreadText = NoThreadText()
    truncated_messages: int = 0
    notices: tuple[str, ...] = ()
    historical_instructions: tuple[HistoricalInstructionProvenance, ...] = ()

    @property
    @abstractmethod
    def imported_messages(self) -> int: ...

    @abstractmethod
    def session_bytes(self, project: Path) -> bytes:
        """The thread's Pi session file, for a thread working in ``project``."""


@dataclass(frozen=True, slots=True, kw_only=True)
class KeptPiSession(ImportedConversation):
    """A native Pi session is already what a thread runs: its file is the session, unchanged."""

    source: Path

    @property
    def imported_messages(self) -> int:
        return self.messages_seen

    def session_bytes(self, project: Path) -> bytes:
        return self.source.read_bytes()
