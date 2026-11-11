from __future__ import annotations

from abc import abstractmethod
from collections.abc import Callable, Iterable, Iterator, Mapping
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Annotated, ClassVar, Self

from pydantic import JsonValue

from agent_comms.context_store import ContextStore
from agent_comms.coordination.service_passes import ServiceReading
from agent_comms.execution.historical_native_inputs import HistoricalNativeAnswers
from agent_comms.execution.transcript_sources import (
    ExternalTranscript,
    StoreRead,
    TranscriptSource,
)
from agent_comms.execution.turn_bound import TurnBound, TurnsSince
from agent_comms.messaging.bus import Bus
from agent_comms.messaging.message_reference import MessageReference
from agent_comms.nominal.declared_family import DeclaredFamily
from agent_comms.nominal.field_codec import (
    ActivityExclude,
    FieldCodec,
    WireExclude,
    WireTransient,
    projected,
)
from agent_comms.planning.held_orders import HeldOrders, LiveHold
from agent_comms.planning.order_work import OrderWork
from agent_comms.planning.orders import (
    EditWorkplaces,
    MergedProgress,
    NoCommitProgress,
    OrderProgress,
    ProblemGoals,
    ProgressRead,
    Todo,
    TodoStore,
)
from agent_comms.planning.problem_number import ProblemNumber
from agent_comms.projects.work_branches import WorkBranchError
from agent_comms.registry.private_registry_guard import RegistryPrivacy
from agent_comms.registry.thread_identity import ThreadIncarnation
from agent_comms.registry.threads import ConversationPreparation, Thread
from agent_comms.transcript.transcript_events import IncomingTranscript, TurnDigest
from agent_comms.transport.tool_declaration import EditCall
from agent_comms.transport.tool_result import ToolResult


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


@dataclass(frozen=True)
class ThreadRead[Result](ConversationPreparation):
    """The original native/external source selection, shared by typed readers."""

    root: Path
    thread: Thread

    def read(self) -> Result:
        return self.thread.prepare_conversation(self)


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
    def for_team(cls, goals: ProblemGoals, teammate: str) -> Iterator[Self]:
        snapshot = goals.registry.snapshot()
        held = HeldOrders.of(goals.todos, snapshot)
        history = goals.todos.board_history()
        pulls = ProgressRead(history)
        workplaces = EditWorkplaces.capture(goals, snapshot, held.holders, pulls)
        yield from cls.judgeable(
            goals.root, held.live, pulls.progress(held.todos, goals.registry), workplaces)

    @classmethod
    def judgeable(cls, root: Path, holds: Iterable[LiveHold],
                  progress: Mapping[str, OrderProgress],
                  workplaces: Mapping[ThreadIncarnation, EditWorkplaces]) -> Iterator[Self]:
        """The reads a verdict can still clear: an order whose PR merged has no head left to
        judge, so its edits are nobody's review and its transcript is never read."""
        return (replace(read, progress=progress[read.todo.id])
                for read in cls.for_orders(root, holds, workplaces)
                if not isinstance(progress[read.todo.id], MergedProgress))

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
