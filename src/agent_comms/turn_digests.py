"""One ordered turn answer, produced by the existing record owners."""

from __future__ import annotations

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


@dataclass(frozen=True)
class TurnDigestRead(ConversationPreparation):
    """Read this unit's own records, never its inherited conversation display."""

    root: Path
    thread: Thread
    last: int = 5

    def read(self) -> ThreadTurns:
        if self.last < 1:
            raise ValueError("last must be positive")
        return ThreadTurns(self.thread.incarnation,
                           self.thread.execution.prepare_conversation(self))

    def native(self) -> tuple[TurnDigest, ...]:
        return self.thread.runtime.history_source(self.root, self.thread.incarnation).turns(self.last)

    def external(self) -> tuple[TurnDigest, ...]:
        with ExternalTranscript(self.root, self.thread.incarnation).reading() as source:
            return source.turns(self.last)
