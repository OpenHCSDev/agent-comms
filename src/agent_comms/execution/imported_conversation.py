"""Original saved-conversation values shared by import and native publication."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path

from agent_comms.execution.imported_session_metadata import ImportObservation
from agent_comms.registry.thread_text import NoThreadText, ThreadText


@dataclass(frozen=True, slots=True, kw_only=True)
class ImportedConversation(ImportObservation, ABC):
    """A session imported as a stopped thread: where it ran and the Pi session it becomes."""

    project: str
    title: ThreadText = NoThreadText()

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
