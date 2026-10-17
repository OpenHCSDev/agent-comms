"""What a commander decided about an order's work, recorded on that order.

A verdict judges one tool call the unit made while holding the order. A send-back
verdict is a strike: it revises the order's Goal text and counts against the unit,
with no warning first. Other misses (a missed deadline, a false done claim, missing
evidence) are strike rows. Nothing here copies the order text, the unit's tool
input or the story: the Goal history, the transcripts and these rows are read.
"""

from __future__ import annotations

import time
from contextlib import closing, contextmanager
from itertools import islice
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, ClassVar

from .declared_family import DeclaredFamily
from .goal_actions import EditGoalAction, GoalActionContext, GoalPrecondition, RuntimeInvocable
from .field_codec import FieldCodec, projected
from .goal_history import GoalHistoryEntry
from .goals import GoalRef
from .transcript_events import ToolStartTranscript
from .plain_text import PlainText
from .thread_identity import RecordedThreadIncarnation
from .typed_table import Column, Index, SqlValue, TypedStore, TypedTable, sql_literal

if TYPE_CHECKING:
    from .goal_management import Goals
    from .goals import Goal
    from .threads import Thread
    from .turn_digests import ThreadTurns


class OrderRecordError(ValueError):
    """The verdict or strike does not match the order or the unit's work."""


class VerdictOutcome(DeclaredFamily, affix="Verdict"):
    """What the commander decided about one tool call."""

    strikes: ClassVar[bool]


class AcceptedVerdict(VerdictOutcome):
    strikes = False


class SentBackVerdict(VerdictOutcome):
    strikes = True


class StrikeCause(DeclaredFamily, affix="StrikeCause"):
    """A miss that is not a judged tool call."""


class MissedDeadlineStrikeCause(StrikeCause):
    pass


class FalseDoneStrikeCause(StrikeCause):
    pass


class MissingEvidenceStrikeCause(StrikeCause):
    pass


class CaughtStage(DeclaredFamily, affix="CaughtStage"):
    """Where the miss was caught; a later stage cost more."""


class ReviewCaughtStage(CaughtStage):
    pass


class InstallCaughtStage(CaughtStage):
    pass


class LiveCaughtStage(CaughtStage):
    pass


class TristanCaughtStage(CaughtStage):
    pass


@dataclass(frozen=True)
class VerdictRow(TypedTable):
    goal: GoalRef = field(metadata={"sql": Column(primary_key=True)})
    tool_call: str = field(metadata={"sql": Column(primary_key=True)})
    outcome: type[VerdictOutcome]
    judge: RecordedThreadIncarnation
    quoted: str
    reason: Annotated[str, PlainText]
    at: float
    unit: str = field(init=False, default=None, compare=False, repr=False, metadata={
        "sql": Column(generated="json_extract(goal,'$.owner.name')")})
    indexes = (Index(("unit", "at")), Index(("at",)))

    @property
    def counted_against(self) -> str:
        return self.goal.owner.name

    def add(self, connection) -> None:
        if self.insert_if_absent(connection).rowcount == 0:
            raise OrderRecordError(f"Tool call {self.tool_call} already has a verdict on this order.")

    @projected(view="wire")
    def line(self) -> str:
        return (f"{self.goal.owner.name}: tool call {self.tool_call} {self.outcome.declared_name} "
                f"by {self.judge.name}: {self.reason}")


@dataclass(frozen=True)
class StrikeRow(TypedTable):
    id: int | None = field(metadata={"sql": Column(primary_key=True)})
    goal: GoalRef
    struck: RecordedThreadIncarnation
    cause: type[StrikeCause]
    what_happened: Annotated[str, PlainText]
    caught: type[CaughtStage]
    judge: RecordedThreadIncarnation
    at: float
    unit: str = field(init=False, default=None, compare=False, repr=False, metadata={
        "sql": Column(generated="json_extract(struck,'$.name')")})
    indexes = (Index(("unit", "at")), Index(("at",)), Index(("goal",)))

    @property
    def counted_against(self) -> str:
        return self.struck.name

    def add(self, connection) -> None:
        self.insert(connection)

    @projected(view="wire")
    def line(self) -> str:
        return (f"{self.struck.name}: {self.cause.declared_name}, caught at "
                f"{self.caught.declared_name} by {self.judge.name}: {self.what_happened}")


@dataclass(frozen=True, kw_only=True)
class SendBackGoalAction(EditGoalAction):
    """The order's commander replaces its text; the Goal counts the revision."""

    judge: RecordedThreadIncarnation
    preserve_wait = False

    def require_author(self, goal: Goal, ctx: GoalActionContext) -> None:
        goal.author.require_commander(self.judge, ctx.goals.registry.snapshot())
        goal.state.require_send_back()


class OrderCall(DeclaredFamily, affix="OrderCall"):
    """One tool call the unit made while holding the order."""


@dataclass(frozen=True)
class UnjudgedOrderCall(OrderCall):
    call: ToolStartTranscript


@dataclass(frozen=True)
class JudgedOrderCall(OrderCall):
    call: ToolStartTranscript
    verdict: VerdictRow


@dataclass(frozen=True)
class HeldOrder:
    """An order Goal of one unit incarnation, current or finished."""

    goals: Goals
    unit: Thread
    reference: GoalRef

    # Verdicts and the order-work read look at this many of the unit's newest turns.
    judged_turns: ClassVar[int] = 20

    @classmethod
    def held(cls, goals: Goals, unit: str, goal_id: str) -> HeldOrder:
        thread = goals.registry.require(unit)
        return cls(goals, thread, GoalRef(thread.incarnation.require_recorded(), goal_id))

    @property
    def records(self) -> OrderRecords:
        return OrderRecords(self.goals.root)

    @property
    def history(self) -> tuple[GoalHistoryEntry, ...]:
        return self.goals.goal_history(self.unit.name, goal_id=self.reference.goal_id)

    def work(self, turns: ThreadTurns) -> tuple[OrderCall, ...]:
        """The unit's tool calls since the order was issued, newest first, with any verdict."""
        history = self.history
        if not history:
            raise OrderRecordError(f"{self.unit.name} never held order {self.reference.goal_id}.")
        verdicts = {row.tool_call: row for row in self.records.verdicts(self.reference)}
        return tuple(
            JudgedOrderCall(call, verdicts[call.tool_call_id]) if call.tool_call_id in verdicts
            else UnjudgedOrderCall(call)
            for turn in turns.turns
            for event in reversed(turn.events)
            for call in event.tool_calls
            if not call.made_before(history[0].observed_at))

    def story(self) -> OrderStory:
        return OrderStory(self.reference, self.history, self.records.verdicts(self.reference),
                          self.records.strikes_on(self.reference))


@dataclass(frozen=True)
class OrderStory:
    """One order's Goal transitions, verdicts and strikes; each owner keeps its own rows."""

    order: GoalRef
    history: tuple[GoalHistoryEntry, ...]
    verdicts: tuple[VerdictRow, ...]
    strikes: tuple[StrikeRow, ...]

    def to_wire(self) -> dict[str, object]:
        return {"order": FieldCodec.encode(self.order),
                "history": [entry.to_wire() for entry in self.history],
                "verdicts": FieldCodec.encode(self.verdicts),
                "strikes": FieldCodec.encode(self.strikes)}


@dataclass(frozen=True)
class JudgedOrder(HeldOrder):
    """The unit's current order, judged by the commander who issued it."""

    goal: Goal
    judge: RecordedThreadIncarnation

    # An accepting reason quotes this many words of the order the call serves.
    serves_words: ClassVar[int] = 4

    @classmethod
    def judged_by(cls, goals: Goals, unit: str, goal_id: str, judge: str) -> JudgedOrder:
        snapshot = goals.registry.snapshot()
        thread = snapshot.require(unit)
        reference = GoalRef(thread.incarnation.require_recorded(), goal_id)
        goal = thread.require_goal(goal_id)
        commander = snapshot.require(judge).incarnation.require_recorded()
        goal.author.require_commander(commander, snapshot)
        return cls(goals, thread, reference, goal, commander)

    def _require_current(self) -> Thread:
        snapshot = self.goals.registry.snapshot()
        if not self.reference.owner.current(snapshot):
            raise OrderRecordError("The order's unit incarnation changed.")
        unit = snapshot.require(self.unit.name)
        goal = unit.require_goal(self.reference.goal_id)
        if goal != self.goal:
            raise OrderRecordError("The order changed before the judgment was written.")
        goal.author.require_commander(self.judge, snapshot)
        return unit

    def _verdict(self, turns: ThreadTurns, outcome: type[VerdictOutcome], tool_call: str,
                 quoted: str, reason: str) -> VerdictRow:
        if not turns.tool_call(tool_call).quotes(quoted):
            raise OrderRecordError(f"Tool call {tool_call} input does not contain the quoted text.")
        return VerdictRow(goal=self.reference, tool_call=tool_call, outcome=outcome,
                          judge=self.judge, quoted=quoted, reason=reason, at=time.time())

    def accept(self, turns: ThreadTurns, tool_call: str, quoted: str, serves: str,
               reason: str) -> VerdictRow:
        words = serves.split()
        if len(words) < self.serves_words or " ".join(words) not in " ".join(self.goal.text.split()):
            raise OrderRecordError(
                f"Quote {self.serves_words}+ exact words of the order the call serves; "
                "a call no order asked for is not accepted.")
        row = self._verdict(turns, AcceptedVerdict, tool_call, quoted, reason)
        with self.goals.writing():
            self._require_current()
            self.records.record(row)
        return row

    def send_back(self, turns: ThreadTurns, tool_call: str, quoted: str, reason: str,
                  text: str) -> Struck:
        row = self._verdict(turns, SentBackVerdict, tool_call, quoted, reason)
        with self.goals.writing() as completed:
            unit = self._require_current()
            with self.records.recording(row) as strikes:
                SendBackGoalAction(expect=GoalPrecondition(expected_goal=self.goal),
                                   judge=self.judge, text=text)._apply_locked(
                    GoalActionContext(self.goals, unit, RuntimeInvocable, completed=completed))
        return Struck.counted(self.goals, row, strikes)

    def strike(self, struck: str, cause: type[StrikeCause], what_happened: str,
               caught: type[CaughtStage]) -> Struck:
        with self.goals.writing():
            self._require_current()
            row = StrikeRow(id=None, goal=self.reference,
                            struck=self.goals.registry.require(struck).incarnation.require_recorded(),
                            cause=cause, what_happened=what_happened, caught=caught,
                            judge=self.judge, at=time.time())
            strikes = self.records.record(row)
        return Struck.counted(self.goals, row, strikes)


@dataclass(frozen=True)
class Struck:
    """A recorded strike and the count it brought its unit to."""

    row: VerdictRow | StrikeRow
    strikes: int

    @classmethod
    def counted(cls, goals: Goals, row: VerdictRow | StrikeRow, strikes: int) -> Struck:
        struck = cls(row, strikes)
        if struck.fired:
            # The limit takes every open order of the struck thread away at once.
            for order in goals.registry.require(row.counted_against).open_orders:
                order.require_current(goals.registry).author.release(goals, order)
        return struck

    @projected(view="wire")
    def fired(self) -> bool:
        return self.strikes >= OrderRecords.strike_limit


@dataclass(frozen=True, slots=True)
class OrderRecords(TypedStore):
    """Verdicts and strikes, indexed by the unit they count against."""

    root: Path
    filename: ClassVar[str] = "order_records.sqlite3"
    rows: ClassVar[tuple[type[TypedTable], ...]] = (VerdictRow, StrikeRow)
    # At this many strikes the unit loses its open order and is fired from the task.
    strike_limit: ClassVar[int] = 2

    @staticmethod
    def _count(connection, unit: str) -> int:
        striking = ", ".join(sql_literal(member) for member in
                             VerdictOutcome.members_with(VerdictOutcome) if member.strikes)
        [count] = SqlValue.for_annotation(int).read(connection.execute(
            f'SELECT (SELECT COUNT(*) FROM "{VerdictRow.declared_name}" '
            f'WHERE "unit" = ? AND "outcome" IN ({striking})) '
            f'+ (SELECT COUNT(*) FROM "{StrikeRow.declared_name}" WHERE "unit" = ?)',
            (unit, unit)))
        return count

    def _rows[Row: TypedTable](self, row: type[Row], **select) -> tuple[Row, ...]:
        if not self.path.exists():
            return ()  # nothing was ever recorded; a read creates nothing
        with self.snapshot() as connection:
            return tuple(row.select(connection, **select))

    def verdicts(self, order: GoalRef) -> tuple[VerdictRow, ...]:
        return self._rows(VerdictRow, goal=order, order_by=("at",))

    def strikes_on(self, order: GoalRef) -> tuple[StrikeRow, ...]:
        return self._rows(StrikeRow, goal=order, order_by=("at",))

    def _newest[Row: TypedTable](self, row: type[Row], limit: int, **select) -> tuple[Row, ...]:
        if not self.path.exists():
            return ()
        with self.snapshot() as connection, closing(row.stream(
                connection, order_by=("at",), descending=True, **select)) as rows:
            return tuple(islice(rows, limit))

    def recent_strikes(self, limit: int) -> tuple[VerdictRow | StrikeRow, ...]:
        """Every strike and send-back, newest first: the fleet's public record."""
        striking = tuple(member for member in VerdictOutcome.members_with(VerdictOutcome)
                         if member.strikes)
        rows = (*self._newest(VerdictRow, limit,
                              where=f'"outcome" IN ({", ".join(map(sql_literal, striking))})'),
                *self._newest(StrikeRow, limit))
        return tuple(sorted(rows, key=lambda row: row.at, reverse=True)[:limit])

    @contextmanager
    def recording(self, row: VerdictRow | StrikeRow):
        """Reject duplicate verdicts before changing an order; commit afterward."""
        with self.writing() as connection:
            row.add(connection)
            yield self._count(connection, row.counted_against)

    def record(self, row: VerdictRow | StrikeRow) -> int:
        with self.recording(row) as strikes:
            return strikes
