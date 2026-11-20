from __future__ import annotations

from abc import abstractmethod
from collections.abc import Callable, Iterable, Iterator, Mapping
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Annotated, ClassVar, Self

from pydantic import JsonValue

from agent_comms.agent_backend import RuntimeLocation
from agent_comms.context_store import ContextStore
from agent_comms.coordination.service_passes import ServiceReading
from agent_comms.execution.historical_native_inputs import HistoricalNativeAnswers
from agent_comms.execution.transcript_sources import (
    CapturedTranscriptAppend,
    ExternalTranscript,
    StoreRead,
    TranscriptSource,
)
from agent_comms.execution.native_record_file import RecordsCut
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
    NoCommitProgress,
    OrderProgress,
    ProblemGoals,
    ProgressRead,
    Todo,
    TodoStore,
)
from agent_comms.planning.problem_number import ProblemNumber
from agent_comms.projects.work_branches import WorkBranchError
from agent_comms.registry.registry_privacy import RegistryPrivacy
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

    def without_tool_results(self, tool_name: str) -> ThreadTurns:
        return replace(
            self,
            turns=tuple(turn.without_tool_results(tool_name) for turn in self.turns),
        )


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
class OrderEditSource:
    """The facts that select one order's original transcript events."""

    root: Path
    thread: ThreadIncarnation
    execution: type[Thread]
    runtime: RuntimeLocation
    bound: TurnsSince
    problem: str


@dataclass(frozen=True)
class OrderEditRead[Result](OrderRead[Result]):
    """A held order's edits: the ones its workplaces own that no verdict names."""
    workplaces: EditWorkplaces
    progress: OrderProgress = NoCommitProgress()

    @property
    def source(self) -> OrderEditSource:
        return OrderEditSource(
            self.root,
            self.thread.incarnation,
            type(self.thread),
            self.thread.runtime,
            self.bound,
            self.todo.id,
        )

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
                if progress[read.todo.id].for_verdict())

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
        return self.owned(self.edit_calls_from_turns(turns))

    def edit_calls_from_turns(self, turns: tuple[TurnDigest, ...]) -> tuple[EditCall, ...]:
        """Latest original call per identity, before this order projects ownership."""
        since = self.bound.at
        edits = {edit.id: edit for turn in reversed(turns)
                 for event in turn.events
                 for edit in event.edit_calls(self.thread.name, self.todo.id, since)}
        return tuple(reversed(edits.values()))


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
class OrderEditCountCut:
    """One holder's order projections and the exact transcript cut that supplied them."""

    reads: tuple[OrderEditCountRead, ...]
    source: RecordsCut
    edits: tuple[tuple[str, tuple[EditCall, ...]], ...]

    @property
    def counts(self) -> tuple[tuple[str, int], ...]:
        """Each order projects ownership from the source calls retained beside it."""
        counts = []
        for read, (problem, edits) in zip(self.reads, self.edits, strict=True):
            if problem != read.todo.id:
                raise ValueError("Retained edit projection belongs to another order")
            counts.append((problem, len(read.owned(edits))))
        return tuple(counts)

    def following(
        self,
        captured: CapturedTranscriptAppend,
        thread: str,
        reads: tuple[OrderEditCountRead, ...],
    ) -> OrderEditCountCut:
        """Advance every order projection from the one appended transcript answer."""
        advanced = []
        for read, (problem, retained) in zip(reads, self.edits, strict=True):
            if problem != read.todo.id:
                raise ValueError("Retained edit projection belongs to another order")
            appended = (
                edit
                for event in captured.events
                for edit in event.edit_calls(thread, problem, read.bound.at)
            )
            by_id = {edit.id: edit for edit in retained}
            by_id.update((edit.id, edit) for edit in appended)
            advanced.append((problem, tuple(by_id.values())))
        return OrderEditCountCut(reads, captured.source, tuple(advanced))

    def reproject(self, reads: tuple[OrderEditCountRead, ...]) -> OrderEditCountCut:
        """Apply current ownership/progress facts to the same retained source events."""
        if self.reads == reads:
            return self
        if tuple(problem for problem, _edits in self.edits) != tuple(
            read.todo.id for read in reads
        ):
            raise ValueError("Retained edit projections belong to other orders")
        return replace(self, reads=reads)


@dataclass(frozen=True)
class OrderEditCountBatch(ThreadRead[OrderEditCountCut]):
    """One holder transcript cut supplying every live order's edit count."""

    reads: tuple[OrderEditCountRead, ...]
    previous: OrderEditCountCut | None

    def following(self, source: TranscriptSource) -> OrderEditCountCut | None:
        """The retained answer advanced under one current source snapshot."""
        if self.previous is None or tuple(
            read.source for read in self.previous.reads
        ) != tuple(read.source for read in self.reads):
            return None
        appended = source.capture_append(self.previous.source)
        if appended is None:
            return None
        return (
            self.previous.reproject(self.reads)
            if appended.source == self.previous.source
            else self.previous.following(appended, self.thread.name, self.reads)
        )

    def native(self) -> OrderEditCountCut:
        source = self.thread.runtime.history_source(self.root, self.thread.incarnation)
        if (following := self.following(source)) is not None:
            return following
        captured = source.capture_turns(TurnsSince(min(read.bound.at for read in self.reads)))
        return OrderEditCountCut(self.reads, captured.source, tuple(
            (read.todo.id, read.edit_calls_from_turns(captured.turns))
            for read in self.reads
        ))

    def external(self) -> OrderEditCountCut:
        transcript = ExternalTranscript(self.root, self.thread.incarnation)
        if (following := self.following(transcript)) is not None:
            return following
        with transcript.reading() as source:
            revision = source.cut()
            return OrderEditCountCut(self.reads, revision, tuple(
                (read.todo.id, tuple(source.unjudged(read.todo, read.bound.at)))
                for read in self.reads
            ))


class OrderEditCountReadings:
    """Retain each holder's counts while its complete order and transcript sources agree."""

    def __init__(self) -> None:
        self.cuts: dict[tuple[ThreadIncarnation, tuple[str, ...]], OrderEditCountCut] = {}
        self.prepared: dict[tuple[ThreadIncarnation, tuple[str, ...]], OrderEditCountCut] = {}

    def begin_pass(self) -> None:
        self.prepared = {}

    def finish_pass(self) -> None:
        self.cuts, self.prepared = self.prepared, {}

    def close(self) -> None:
        self.cuts = {}
        self.prepared = {}

    def read_all(self, reads: Iterable[OrderEditCountRead]) -> dict[str, int]:
        counts: dict[str, int] = {}
        by_holder: dict[ThreadIncarnation, list[OrderEditCountRead]] = {}
        for read in reads:
            if read.progress.unjudged(lambda: 1):
                by_holder.setdefault(read.thread.incarnation, []).append(read)
            else:
                counts[read.todo.id] = 0
        for holder, selected in by_holder.items():
            key = holder, tuple(read.todo.id for read in selected)
            cut = OrderEditCountBatch(
                selected[0].root, selected[0].thread, tuple(selected),
                self.prepared.get(key, self.cuts.get(key)),
            ).read()
            self.prepared[key] = cut
            counts.update(cut.counts)
        return counts


@dataclass(frozen=True)
class OrderToolCallCountRead(OrderRead[int]):
    """Original tool starts since a captured head/order cut, without loading external bodies."""

    def from_turns(self, turns: tuple[TurnDigest, ...]) -> int:
        return len(OrderWork.read(
            self.todo.id, self.thread.incarnation, self.bound.at, turns,
        ).calls)

    def from_external(self, source: StoreRead) -> int:
        return source.tool_call_count(self.bound.at)
