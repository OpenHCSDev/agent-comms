"""One ordered turn answer, produced by the existing record owners."""

from __future__ import annotations

from agent_comms.field_codec import ActivityExclude

from abc import abstractmethod
from collections.abc import Callable, Iterable, Iterator, Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, ClassVar, Self

from pydantic import JsonValue

from .bus import Bus
from .context_store import ContextStore
from .declared_family import DeclaredFamily
from .errors import ToolRefusedError
from .external_transcript import ExternalTranscript, StoreRead
from .field_codec import FieldCodec, WireExclude, WireTransient, projected
from .held_orders import HeldOrders, LiveHold
from .historical_native_inputs import HistoricalNativeAnswers
from .message_reference import MessageReference
from .private_registry_guard import RegistryPrivacy
from .order_work import OrderWork
from .problem_number import ProblemNumber
from .storage_rules import EditWorkplaces
from .thread_execution import ConversationPreparation
from .thread_identity import ThreadIncarnation
from .threads import Thread
from .todos import NoCommitProgress, OrderProgress, ProgressRead, Todo, TodoStore
from .tool_declaration import EditCall
from .tool_result import ToolResult
from .service_passes import ServiceReading
from .transcript_events import IncomingTranscript, TurnDigest
from .turn_item import NativeTurnId
from .work_branches import WorkBranchError

if TYPE_CHECKING:
    from .comms import Comms
    from .registry_document import RegistrySnapshot
    from .transcript_sources import TranscriptSource


class TurnView(DeclaredFamily, affix="TurnView"):
    """The original facts or a bounded activity projection of the same read."""

    @classmethod
    @abstractmethod
    def project(cls, answer: ThreadTurns) -> JsonValue: ...


class ActivityTurnView(TurnView):
    turn_limit: ClassVar[int] = 5

    @classmethod
    def project(cls, answer: ThreadTurns) -> JsonValue:
        selected = answer.turns[:cls.turn_limit]
        page = replace(answer, turns=selected, older=answer.older.older(selected))
        return FieldCodec.project(page, ActivityExclude)


class OriginalTurnView(TurnView):
    @classmethod
    def project(cls, answer: ThreadTurns) -> JsonValue:
        return FieldCodec.encode(answer)


@dataclass(frozen=True)
class ThreadTurns(ToolResult):
    thread: ThreadIncarnation
    turns: tuple[TurnDigest, ...]
    older: TurnBound
    detail: type[TurnView] = ActivityTurnView

    def to_wire(self) -> JsonValue:
        return self.detail.project(self)


@dataclass(frozen=True)
class UnavailableOrderEdits(ToolResult):
    """One original-edit source failed at its foreign I/O boundary; other reads survive."""

    problem: ProblemNumber
    owner: ThreadIncarnation
    cause: Annotated[WorkBranchError | OSError, WireTransient()]

    @projected(view=WireExclude)
    def error(self) -> str:
        return str(self.cause)


@dataclass(frozen=True)
class UnjudgedEdits(ServiceReading):
    edits: tuple[EditCall, ...]
    unavailable: tuple[UnavailableOrderEdits, ...] = ()

    @projected(view=WireExclude)
    def complete(self) -> bool:
        return not self.unavailable


class TurnBound(DeclaredFamily):
    """Which of a unit's turns, read newest first, a reader returns."""

    def skips(self, turn: TurnDigest) -> bool:
        return False

    def page_count(self, turns: tuple[TurnDigest, ...]) -> int:
        return len(turns)

    def displayed(self, turns: tuple[TurnDigest, ...]) -> tuple[TurnDigest, ...]:
        """The returned turns a reader sees as text, so their selected messages are decoded."""
        return turns

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
class RecentTurnsSince(TurnsSince):
    """Keep the recent turn window as well as every turn since an order's review cut."""

    recent: LastTurns = field(kw_only=True)

    def take(self, turns: Iterable[TurnDigest]) -> tuple[TurnDigest, ...]:
        remaining = iter(turns)
        return (*self.recent.take(remaining), *super().take(remaining))

    def displayed(self, turns: tuple[TurnDigest, ...]) -> tuple[TurnDigest, ...]:
        """Only the recent window is read as text; turns behind it answer for their calls."""
        return self.recent.take(turns)


@dataclass(frozen=True)
class ThreadRead[Result](ConversationPreparation):
    """The original native/external source selection, shared by typed readers."""

    root: Path
    thread: Thread

    def read(self) -> Result:
        return self.thread.execution.prepare_conversation(self)


@dataclass(frozen=True)
class InputAnswerRead(ThreadRead[frozenset[MessageReference]]):
    references: tuple[MessageReference, ...]
    privacy: RegistryPrivacy

    def read(self) -> frozenset[MessageReference]:
        if not self.references:
            return frozenset()
        return super().read()

    def native(self) -> frozenset[MessageReference]:
        return self.privacy.native_answered(
            HistoricalNativeAnswers(self.root, self.thread.incarnation, self.privacy), self.references)

    def external(self) -> frozenset[MessageReference]:
        return ExternalTranscript(self.root, self.thread.incarnation).answered_inputs(self.references)


@dataclass(frozen=True)
class TranscriptRead[Result](ThreadRead[Result]):
    bound: TurnBound

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
            ContextStore(self.root), tuple(turn.turn_id for turn in self.bound.displayed(turns)))
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
    progress: OrderProgress = NoCommitProgress()

    def when_readable(self, operation: Callable[[], Result]) -> Result:
        self.progress.require_read()
        return operation()

    def read(self) -> Result:
        return self.when_readable(super().read)

    @classmethod
    def for_commander(cls, comms: Comms, commander: str) -> Iterator[Self]:
        snapshot = comms.registry.snapshot()
        held = HeldOrders.of(comms.goals.todos, snapshot, commander)
        history = comms.goals.todos.history_for_commander(snapshot.require(commander).incarnation)
        pulls = ProgressRead(history)
        workplaces = EditWorkplaces.capture(comms, snapshot, held.live, pulls)
        yield from cls.judgeable(
            comms.root, held.live, pulls.progress(held.todos, comms.registry), workplaces)

    @classmethod
    def judgeable(cls, root: Path, holds: Iterable[LiveHold],
                  progress: Mapping[str, OrderProgress],
                  workplaces: Mapping[ThreadIncarnation, EditWorkplaces]) -> Iterator[Self]:
        """The reads a verdict can still clear: an order whose PR merged has no head left to
        judge, so its edits are nobody's review and its transcript is never read."""
        return (replace(read, progress=progress[read.todo.id])
                for read in cls.for_orders(root, holds, workplaces)
                if progress[read.todo.id].judgeable)

    @classmethod
    def for_orders(cls, root: Path, holds: Iterable[LiveHold],
                   workplaces: Mapping[ThreadIncarnation, EditWorkplaces]) -> Iterator[Self]:
        """Read each live hold's transcript from its canonical review cut; each edit is
        read under the one order its workplace names."""
        store = TodoStore(root)
        for hold in holds:
            yield cls(
                root, hold.holder, TurnsSince(store.unjudged_since(hold.todo)), hold.todo,
                workplaces[hold.holder.incarnation],
            )

    def owned(self, edits: Iterable[EditCall]) -> tuple[EditCall, ...]:
        judged = frozenset(self.todo.judged_calls)
        return tuple(replace(edit, problem=self.todo.id) for edit in edits
                     if edit.id not in judged
                     and self.workplaces.problems_for(edit.path) == frozenset((self.todo.id,)))

    @abstractmethod
    def project_edits(self, edits: tuple[EditCall, ...]) -> Result: ...

    def from_turns(self, turns: tuple[TurnDigest, ...]) -> Result:
        return self.when_readable(lambda: self.project_edits(self.edits_from_turns(turns)))

    def from_external(self, source: StoreRead) -> Result:
        return self.when_readable(lambda: self.project_edits(
            self.owned(source.unjudged(self.todo, self.bound.at))))

    def edits_from_turns(self, turns: tuple[TurnDigest, ...]) -> tuple[EditCall, ...]:
        since = self.bound.at
        edits = {edit.id: edit for turn in reversed(turns)
                 for event in turn.events
                 for edit in event.edit_calls(self.thread.name, self.todo.id, since)}
        return self.owned(reversed(edits.values()))




class OrderEditsRead(OrderEditRead[tuple[EditCall, ...]]):
    """Original unjudged calls; external events are selected before decoding."""

    def project_edits(self, edits: tuple[EditCall, ...]) -> tuple[EditCall, ...]:
        return edits


class OrderEditCountRead(OrderEditRead[int]):
    """Load counts the edits each order owns; ownership reads each edit's path."""

    def when_readable(self, operation: Callable[[], int]) -> int:
        return self.progress.unjudged(operation)

    def project_edits(self, edits: tuple[EditCall, ...]) -> int:
        return len(edits)


@dataclass(frozen=True)
class OrderToolCallCountRead(OrderRead[int]):
    """Original tool starts since a captured head/order cut, without loading external bodies."""

    def from_turns(self, turns: tuple[TurnDigest, ...]) -> int:
        return len(OrderWork.read(
            self.todo.id, self.thread.incarnation, self.bound.at, turns,
        ).calls)

    def from_external(self, source: StoreRead) -> int:
        return source.tool_call_count(self.bound.at)
