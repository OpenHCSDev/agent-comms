"""One ordered turn answer, produced by the existing record owners."""

from __future__ import annotations

from abc import abstractmethod
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Self

from .declared_family import DeclaredFamily
from .errors import ToolRefusedError
from .external_transcript import ExternalTranscript, StoreRead
from .order_work import OrderWork
from .thread_execution import ConversationPreparation
from .thread_identity import ThreadIncarnation
from .tool_declaration import EditCall
from .tool_result import ToolResult
from .bus import Bus
from .context_store import ContextStore
from .transcript_events import IncomingTranscript, TurnDigest

if TYPE_CHECKING:
    from .comms import Comms
    from .threads import Thread
    from .transcript_sources import TranscriptSource
    from .todos import LiveHold, Todo


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
        return self.from_native(source, source.turns(self.bound))

    def from_native(self, source: TranscriptSource, turns: tuple[TurnDigest, ...]) -> Result:
        return self.from_turns(turns)

    def external(self) -> Result:
        with ExternalTranscript(self.root, self.thread.incarnation).reading() as source:
            return self.from_external(source)

    @abstractmethod
    def from_turns(self, turns: tuple[TurnDigest, ...]) -> Result: ...

    def from_external(self, source: StoreRead) -> Result:
        return self.from_turns(source.turns(self.bound))


class TurnDigestRead(TranscriptRead[ThreadTurns]):
    """Read this unit's own records, never its inherited conversation display."""

    def from_native(self, source: TranscriptSource, turns: tuple[TurnDigest, ...]) -> ThreadTurns:
        """A woken input reads as the messages its wake selected, from their bus records."""
        recorded = source.recorded_contexts(
            ContextStore(self.root), tuple(turn.turn_id for turn in turns))
        selected = {turn_id: manifest.wire_sources() for turn_id, manifest in recorded.items()}
        references = tuple(dict.fromkeys(ref for refs in selected.values() for ref in refs))
        deliveries = (Bus(Bus.path_in(self.root)).deliveries_for_references(references)
                      if references else ())
        # The owner's own sent outcomes share the wake's provenance; their sender tells them apart.
        incoming = {delivery.reference: IncomingTranscript.of(delivery, reader=self.thread.name)
                    for delivery in deliveries
                    if delivery.audience.sender_lookup != self.thread.incarnation.lookup}
        return self.from_turns(tuple(
            turn.with_selected(chosen) if (chosen := tuple(
                incoming[ref] for ref in selected.get(turn.turn_id, ()) if ref in incoming))
            else turn
            for turn in turns))

    def from_turns(self, turns: tuple[TurnDigest, ...]) -> ThreadTurns:
        return ThreadTurns(self.thread.incarnation, turns, self.bound.older(turns))


@dataclass(frozen=True)
class OrderRead[Result](TranscriptRead[Result]):
    bound: TurnsSince
    todo: Todo

    @classmethod
    def for_commander(cls, comms: Comms, commander: str) -> Iterator[Self]:
        from .todos import HeldOrders

        yield from cls.for_orders(comms.root, HeldOrders.read(comms, commander).live)

    @classmethod
    def for_orders(cls, root: Path, holds: Iterable[LiveHold]) -> Iterator[Self]:
        """Read each live hold's transcript from its canonical review cut."""
        from .todos import TodoStore

        store = TodoStore(root)
        for hold in holds:
            yield cls(
                root, hold.holder, TurnsSince(store.unjudged_since(hold.todo)), hold.todo
            )

    def edits_from_turns(self, turns: tuple[TurnDigest, ...]) -> tuple[EditCall, ...]:
        judged = frozenset(self.todo.judged_calls)
        since = self.bound.at
        edits = {edit.id: edit for turn in reversed(turns)
                 for event in turn.events
                 for edit in event.edit_calls(self.thread.name, self.todo.id, since)
                 if edit.id not in judged}
        return tuple(reversed(edits.values()))


class OrderEditsRead(OrderRead[tuple[EditCall, ...]]):
    """Original unjudged calls; external events are selected before decoding."""

    def from_turns(self, turns: tuple[TurnDigest, ...]) -> tuple[EditCall, ...]:
        return self.edits_from_turns(turns)

    def from_external(self, source: StoreRead) -> tuple[EditCall, ...]:
        return tuple(source.unjudged(self.todo, self.bound.at))


class OrderEditCountRead(OrderRead[int]):
    """Load needs a count, never external event bodies or edit hunks."""

    @classmethod
    def count(cls, comms: Comms, commander: str) -> int:
        return sum(read.read() for read in cls.for_commander(comms, commander))

    def from_turns(self, turns: tuple[TurnDigest, ...]) -> int:
        return len(self.edits_from_turns(turns))

    def from_external(self, source: StoreRead) -> int:
        return source.unjudged_count(self.todo, self.bound.at)


@dataclass(frozen=True)
class OrderToolCallCountRead(OrderRead[int]):
    """Original tool starts since a captured head/order cut, without loading external bodies."""

    def from_turns(self, turns: tuple[TurnDigest, ...]) -> int:
        return len(OrderWork.read(
            self.todo.id, self.thread.incarnation, self.bound.at, turns,
        ).calls)

    def from_external(self, source: StoreRead) -> int:
        return source.tool_call_count(self.bound.at)
