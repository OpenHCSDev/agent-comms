"""One ordered turn answer, produced by the existing record owners."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from .thread_execution import ConversationPreparation
from .thread_identity import ThreadIncarnation
from .external_transcript import ExternalTranscript
from .transcript_events import ToolStartTranscript, TurnDigest

if TYPE_CHECKING:
    from .threads import Thread


@dataclass(frozen=True)
class ThreadTurns:
    thread: ThreadIncarnation
    turns: tuple[TurnDigest, ...]

    def tool_call(self, tool_call_id: str) -> ToolStartTranscript:
        for turn in self.turns:
            for event in turn.events:
                for call in event.tool_calls:
                    if call.tool_call_id == tool_call_id:
                        return call
        raise ValueError(f"{self.thread.name} made no tool call {tool_call_id} "
                         f"in its last {len(self.turns)} turns.")


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
        return tuple(turn for transcript in self.thread.native_transcripts
                     for turn in transcript.turns(self.last))

    def external(self) -> tuple[TurnDigest, ...]:
        with ExternalTranscript(self.root, self.thread.incarnation).reading() as source:
            return source.turns(self.last)
