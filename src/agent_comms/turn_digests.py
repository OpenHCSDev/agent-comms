"""One ordered turn answer, produced by the existing record owners."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from .thread_execution import ConversationPreparation
from .thread_identity import ThreadIncarnation
from .transcript_events import TranscriptEvent

if TYPE_CHECKING:
    from .threads import Thread


@dataclass(frozen=True)
class TurnDigest:
    turn_id: str
    events: tuple[TranscriptEvent, ...]


@dataclass(frozen=True)
class ThreadTurns:
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
        from .native_transcript import NativeTranscript

        if not self.thread.session_file:
            return ()
        return NativeTranscript(Path(self.thread.session_file)).turns(self.last)

    def external(self) -> tuple[TurnDigest, ...]:
        from .external_transcript import ExternalTranscript

        with ExternalTranscript(self.root, self.thread.incarnation).reading() as source:
            return source.turns(self.last)
