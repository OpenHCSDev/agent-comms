"""One ordered turn answer, produced by the existing record owners."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Self

from .channel_targets import ConversationPreparation
from .errors import ToolRefusedError
from .external_transcript import ExternalTranscript
from .thread_identity import ThreadIncarnation
from .tool_declaration import EditCall
from .tool_result import ToolResult
from .transcript_events import TurnDigest

if TYPE_CHECKING:
    from .comms import Comms
    from .threads import Thread
    from .todos import Todo


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
class TranscriptRead[Result](ConversationPreparation[Result]):
    """The original native/external source selection, shared by typed readers."""

    root: Path
    thread: Thread
    bound: TurnBound

    def read(self) -> Result:
        return self.thread.execution.prepare_conversation(self)

    def native(self) -> Result:
        source = self.thread.runtime.history_source(self.root, self.thread.incarnation)
        turns = source.turns(self.bound)
        return self.from_turns(turns)

    def external(self) -> Result:
        with ExternalTranscript(self.root, self.thread.incarnation).reading() as source:
            return self.from_external(source)

    @abstractmethod
    def from_turns(self, turns: tuple[TurnDigest, ...]) -> Result: ...

    def from_external(self, source) -> Result:
        return self.from_turns(source.turns(self.bound))


class TurnDigestRead(TranscriptRead[ThreadTurns]):
    """Read this unit's own records, never its inherited conversation display."""

    def from_turns(self, turns: tuple[TurnDigest, ...]) -> ThreadTurns:
        return ThreadTurns(self.thread.incarnation, turns)


@dataclass(frozen=True)
class OrderRead[Result](TranscriptRead[Result]):
    todo: Todo

    @classmethod
    def for_commander(cls, comms: Comms, commander: str) -> Iterator[Self]:
        actor = comms.registry.require(commander).incarnation
        for todo in comms.goals.todos.held_for_commander(actor):
            assignment = todo.held()
            holder = comms.registry.require(assignment.owner)
            if holder.incarnation != assignment.owner_identity:
                raise ToolRefusedError(f"Order {todo.id} holder incarnation no longer exists")
            yield cls(comms.root, holder, TurnsSince(todo.order.checked().at.recorded_at("An order")), todo)

    def edits_from_turns(self, turns: tuple[TurnDigest, ...]) -> tuple[EditCall, ...]:
        judged = {verdict.tool_call_id for verdict in self.todo.verdicts}
        since = self.todo.order.checked().at.recorded_at("An order")
        edits = {edit.id: edit for turn in reversed(turns)
                 for event in turn.events
                 for edit in event.edit_calls(self.thread.name, self.todo.id, since)
                 if edit.id not in judged}
        return tuple(reversed(edits.values()))


class OrderEditsRead(OrderRead[tuple[EditCall, ...]]):
    """Original unjudged calls; external events are selected before decoding."""

    def from_turns(self, turns: tuple[TurnDigest, ...]) -> tuple[EditCall, ...]:
        return self.edits_from_turns(turns)

    def from_external(self, source) -> tuple[EditCall, ...]:
        return tuple(source.unjudged(self.todo))


class OrderEditCountRead(OrderRead[int]):
    """Load needs a count, never external event bodies or edit hunks."""

    @classmethod
    def count(cls, comms: Comms, commander: str) -> int:
        return sum(read.read() for read in cls.for_commander(comms, commander))

    def from_turns(self, turns: tuple[TurnDigest, ...]) -> int:
        return len(self.edits_from_turns(turns))

    def from_external(self, source) -> int:
        return source.unjudged_count(self.todo)
