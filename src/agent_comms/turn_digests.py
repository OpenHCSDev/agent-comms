"""One ordered turn answer, produced by the existing record owners."""

from __future__ import annotations

from abc import abstractmethod
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Self

from .todos import ProgressRead, TodoStore

from .turn_item import NativeTurnId
from .declared_family import DeclaredFamily
from .errors import ToolRefusedError
from .external_transcript import ExternalTranscript, StoreRead
from .held_orders import HeldOrders, LiveHold
from .order_work import OrderWork
from .storage_rules import StorageRule
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
    from .todos import OrderProgress, Todo


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

    turn_id: NativeTurnId = field(kw_only=True)

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
                    if delivery.sender_lookup != self.thread.incarnation.lookup}
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


@dataclass(frozen=True)
class OrderEditRead[Result](OrderRead[Result]):
    """A held order's edits: the ones its workplaces own that no verdict names."""
    workplaces: EditWorkplaces

    @classmethod
    def for_commander(cls, comms: Comms, commander: str) -> Iterator[Self]:
        held = HeldOrders.read(comms, commander)
        yield from cls.judgeable(
            comms.root, held.live, ProgressRead().progress(held.todos, comms.registry))

    @classmethod
    def judgeable(cls, root: Path, holds: Iterable[LiveHold],
                  progress: Mapping[str, OrderProgress]) -> Iterator[Self]:
        """The reads a verdict can still clear: an order whose PR merged has no head left to
        judge, so its edits are nobody's review and its transcript is never read."""
        return (read for read in cls.for_orders(root, holds) if progress[read.todo.id].judgeable)

    @classmethod
    def for_orders(cls, root: Path, holds: Iterable[LiveHold]) -> Iterator[Self]:
        """Read each live hold's transcript from its canonical review cut; each edit is
        read under the one order its workplace names."""
        store = TodoStore(root)
        rule = StorageRule.load(root)
        holds = tuple(holds)
        for hold in holds:
            yield cls(
                root, hold.holder, TurnsSince(store.unjudged_since(hold.todo)), hold.todo,
                EditWorkplaces.of(rule, hold.holder, tuple(
                    other.todo for other in holds if other.holder.name == hold.holder.name)),
            )

    def owned(self, edits: Iterable[EditCall]) -> tuple[EditCall, ...]:
        judged = frozenset(self.todo.judged_calls)
        return tuple(edit for edit in edits if edit.id not in judged
                     and self.workplaces.order_of(edit.path) == self.todo.id)

    def edits_from_turns(self, turns: tuple[TurnDigest, ...]) -> tuple[EditCall, ...]:
        since = self.bound.at
        edits = {edit.id: edit for turn in reversed(turns)
                 for event in turn.events
                 for edit in event.edit_calls(self.thread.name, self.todo.id, since)}
        return self.owned(reversed(edits.values()))


@dataclass(frozen=True)
class EditWorkplaces:
    """Which of one unit's held orders an edit belongs to. An edit inside a problem checkout
    belongs to that problem's order; any other path (scratch, a shared checkout) belongs to
    the order the unit was given last. Each edit is read under exactly one order."""

    checkouts: dict[str, tuple[Path, ...]]
    latest: str

    @classmethod
    def of(cls, rule: StorageRule, holder: Thread, todos: tuple[Todo, ...]) -> Self:
        roots = rule.roots_of(holder)
        return cls({todo.id: rule.problem_checkouts(roots, todo.id) for todo in todos},
                   max(todos, key=lambda todo: todo.order.checked().at.recorded_at("An order")).id)

    def order_of(self, path: str) -> str:
        edited = Path(path)
        return next((problem for problem, checkouts in self.checkouts.items()
                     if any(edited.is_relative_to(checkout) for checkout in checkouts)),
                    self.latest)


class OrderEditsRead(OrderEditRead[tuple[EditCall, ...]]):
    """Original unjudged calls; external events are selected before decoding."""

    def from_turns(self, turns: tuple[TurnDigest, ...]) -> tuple[EditCall, ...]:
        return self.edits_from_turns(turns)

    def from_external(self, source: StoreRead) -> tuple[EditCall, ...]:
        return self.owned(source.unjudged(self.todo, self.bound.at))


class OrderEditCountRead(OrderEditRead[int]):
    """Load counts the edits each order owns; ownership reads each edit's path."""

    def from_turns(self, turns: tuple[TurnDigest, ...]) -> int:
        return len(self.edits_from_turns(turns))

    def from_external(self, source: StoreRead) -> int:
        return len(self.owned(source.unjudged(self.todo, self.bound.at)))


@dataclass(frozen=True)
class OrderToolCallCountRead(OrderRead[int]):
    """Original tool starts since a captured head/order cut, without loading external bodies."""

    def from_turns(self, turns: tuple[TurnDigest, ...]) -> int:
        return len(OrderWork.read(
            self.todo.id, self.thread.incarnation, self.bound.at, turns,
        ).calls)

    def from_external(self, source: StoreRead) -> int:
        return source.tool_call_count(self.bound.at)
