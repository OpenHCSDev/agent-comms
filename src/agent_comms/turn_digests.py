"""One ordered turn answer, produced by the existing record owners."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from .thread_execution import ConversationPreparation
from .thread_identity import ThreadIncarnation
from .external_transcript import ExternalTranscript
from .transcript_events import TurnDigest
from .tool_result import ToolResult

if TYPE_CHECKING:
    from .threads import Thread


@dataclass(frozen=True)
class ThreadTurns(ToolResult):
    thread: ThreadIncarnation
    turns: tuple[TurnDigest, ...]


class TurnBound(ABC):
    """Which of a unit's turns, read newest first, a reader returns."""

    @abstractmethod
    def includes(self, turn: TurnDigest) -> bool:
        """Whether this turn belongs; the first turn refused ends the read."""

    @abstractmethod
    def full(self, taken: int) -> bool:
        """Whether ``taken`` turns end the read."""


@dataclass(frozen=True)
class LastTurns(TurnBound):
    count: int = 5

    def __post_init__(self) -> None:
        if self.count < 1:
            raise ValueError("last must be positive")

    def includes(self, turn: TurnDigest) -> bool:
        return True

    def full(self, taken: int) -> bool:
        return taken >= self.count


@dataclass(frozen=True)
class TurnsSince(TurnBound):
    at: float

    def includes(self, turn: TurnDigest) -> bool:
        return not turn.before(self.at)

    def full(self, taken: int) -> bool:
        return False


@dataclass(frozen=True)
class TurnDigestRead(ConversationPreparation):
    """Read this unit's own records, never its inherited conversation display."""

    root: Path
    thread: Thread
    bound: TurnBound

    def read(self) -> ThreadTurns:
        return ThreadTurns(self.thread.incarnation,
                           self.thread.execution.prepare_conversation(self))

    def native(self) -> tuple[TurnDigest, ...]:
        return self.thread.runtime.history_source(self.root, self.thread.incarnation).turns(self.bound)

    def external(self) -> tuple[TurnDigest, ...]:
        with ExternalTranscript(self.root, self.thread.incarnation).reading() as source:
            return source.turns(self.bound)
