"""One ordered turn answer, produced by the existing record owners."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from .thread_execution import ConversationPreparation
from .thread_identity import ThreadIncarnation
from .external_transcript import ExternalTranscript
from .transcript_events import TurnDigest
from .tool_result import ToolResult
from .tool_declaration import EditCall

if TYPE_CHECKING:
    from .threads import Thread


@dataclass(frozen=True)
class ThreadTurns(ToolResult):
    thread: ThreadIncarnation
    turns: tuple[TurnDigest, ...]


@dataclass(frozen=True)
class UnjudgedEdits(ToolResult):
    edits: tuple[EditCall, ...]


class TurnBound(ABC):
    """Which of a unit's turns, read newest first, a reader returns."""

    @abstractmethod
    def includes(self, turn: TurnDigest) -> bool:
        """Whether this turn belongs; the first turn refused ends the read."""

    @abstractmethod
    def full(self, taken: int) -> bool:
        """Whether ``taken`` turns end the read."""

    def take(self, turns: Iterable[TurnDigest]) -> tuple[TurnDigest, ...]:
        """The bounded turns of a newest-first stream, reading no further than the bound needs."""
        taken: list[TurnDigest] = []
        for turn in turns:
            if not self.includes(turn):
                break
            taken.append(turn)
            if self.full(len(taken)):
                break
        return tuple(taken)


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

    def unjudged(self, todo) -> tuple[EditCall, ...]:
        """Each native call once, judged only by this order's own verdicts."""
        since = todo.order.at.recorded_at("An order")
        edits = {edit.id: edit for turn in reversed(self.read().turns)
                 for event in turn.events
                 for edit in event.edit_calls(self.thread.name, todo.id, since)
                 if not any(verdict.judges(edit.id) for verdict in todo.verdicts)}
        return tuple(reversed(edits.values()))

    def native(self) -> tuple[TurnDigest, ...]:
        return self.thread.runtime.history_source(self.root, self.thread.incarnation).turns(self.bound)

    def external(self) -> tuple[TurnDigest, ...]:
        with ExternalTranscript(self.root, self.thread.incarnation).reading() as source:
            return source.turns(self.bound)
