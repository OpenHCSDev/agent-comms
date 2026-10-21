"""One ordered turn answer, produced by the existing record owners."""

from __future__ import annotations

from abc import abstractmethod
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Self

from .declared_family import DeclaredFamily
from .errors import ToolRefusedError
from .external_transcript import ExternalTranscript
from .thread_execution import ConversationPreparation
from .thread_identity import ThreadIncarnation
from .tool_declaration import EditCall
from .tool_result import ToolResult
from .transcript_events import TurnDigest

if TYPE_CHECKING:
    from .comms import Comms
    from .registry_document import RegistrySnapshot
    from .threads import Thread
    from .todos import Todo


@dataclass(frozen=True)
class ThreadTurns(ToolResult):
    thread: ThreadIncarnation
    turns: tuple[TurnDigest, ...]
    older: TurnBound


@dataclass(frozen=True)
class UnjudgedEdits(ToolResult):
    edits: tuple[EditCall, ...]


class TurnBound(DeclaredFamily):
    """Which of a unit's turns, read newest first, a reader returns."""

    def skips(self, turn: TurnDigest) -> bool:
        return False

    def page_count(self, turns: tuple[TurnDigest, ...]) -> int:
        return len(turns)

    def older(self, turns: tuple[TurnDigest, ...]) -> TurnBound:
        """A recorded identity continues even across undated or equal-time turns."""
        if not turns:
            return self
        return TurnsOlderThan(turn_id=turns[-1].turn_id, count=self.page_count(turns))

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
            if self.skips(turn):
                continue
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

    def page_count(self, turns: tuple[TurnDigest, ...]) -> int:
        return self.count


@dataclass(frozen=True)
class TurnsBefore(LastTurns):
    at: float = field(kw_only=True)

    def skips(self, turn: TurnDigest) -> bool:
        return not turn.starts_before(self.at)


@dataclass(frozen=True)
class TurnsOlderThan(LastTurns):
    """Read older than an already returned original turn, not an offset from the newest."""

    turn_id: str = field(kw_only=True)

    def take(self, turns: Iterable[TurnDigest]) -> tuple[TurnDigest, ...]:
        remaining = iter(turns)
        for turn in remaining:
            if turn.turn_id == self.turn_id:
                return super().take(remaining)
        raise ToolRefusedError(f"Continuation turn {self.turn_id!r} is not in the recorded transcript.")


@dataclass(frozen=True)
class TurnsSince(TurnBound):
    at: float

    def includes(self, turn: TurnDigest) -> bool:
        return not turn.before(self.at)

    def full(self, taken: int) -> bool:
        return False


@dataclass(frozen=True)
class TranscriptRead[Result](ConversationPreparation):
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
        return ThreadTurns(self.thread.incarnation, turns, self.bound.older(turns))


@dataclass(frozen=True)
class OrderRead[Result](TranscriptRead[Result]):
    todo: Todo

    @classmethod
    def for_commander(cls, comms: Comms, commander: str) -> Iterator[Self]:
        snapshot = comms.registry.snapshot()
        actor = snapshot.require(commander).incarnation
        yield from cls.for_orders(
            comms.root, snapshot, comms.goals.todos.held_for_commander(actor)
        )

    @classmethod
    def for_orders(
        cls, root: Path, snapshot: RegistrySnapshot, todos: Iterable[Todo]
    ) -> Iterator[Self]:
        """Read these orders against their captured roster, without reopening the registry."""
        for todo in todos:
            assignment = todo.held()
            holder = snapshot.require(assignment.owner)
            if holder.incarnation != assignment.owner_identity:
                raise ToolRefusedError(f"Order {todo.id} holder incarnation no longer exists")
            yield cls(
                root, holder, TurnsSince(todo.evidence_since), todo
            )

    def edits_from_turns(self, turns: tuple[TurnDigest, ...]) -> tuple[EditCall, ...]:
        judged = set(self.todo.judged_call_ids)
        since = self.todo.evidence_since
        edits = {edit.id: edit for turn in reversed(turns)
                 for event in turn.events
                 for edit in event.edit_calls(self.thread.name, self.todo.id, since)
                 if edit.id not in judged}
        return tuple(reversed(edits.values()))


class OrderEditsRead(OrderRead[tuple[EditCall, ...]]):
    """Original edits since the head verdict; external events are selected before decoding."""

    def from_turns(self, turns: tuple[TurnDigest, ...]) -> tuple[EditCall, ...]:
        return self.edits_from_turns(turns)

    def from_external(self, source) -> tuple[EditCall, ...]:
        return tuple(source.unjudged(self.todo))
