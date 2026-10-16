"""What a commander decided about an order's work, recorded on that order.

A verdict judges one tool call the unit made while holding the order. A send-back
verdict is a strike: it revises the order's Goal text and counts against the unit,
with no warning first. Other misses (a missed deadline, a false done claim, missing
evidence) are strike rows. Nothing here copies the order text, the unit's tool
input or the story: the Goal history, the transcripts and these rows are read.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, ClassVar

from .declared_family import DeclaredFamily
from .field_codec import projected
from .goal_actions import GoalAction, GoalActionContext, GoalPrecondition, RuntimeInvocable
from .goals import GoalRef
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
    indexes = (Index(("unit", "at")),)

    @property
    def counted_against(self) -> str:
        return self.goal.owner.name

    def add(self, connection) -> None:
        if not self.insert_if_absent(connection).rowcount:
            raise OrderRecordError(f"Tool call {self.tool_call} already has a verdict on this order.")


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
    indexes = (Index(("unit", "at")),)

    @property
    def counted_against(self) -> str:
        return self.struck.name

    def add(self, connection) -> None:
        self.insert(connection)


@dataclass(frozen=True, kw_only=True)
class SendBackGoalAction(GoalAction, RuntimeInvocable):
    """The order's commander replaces its text; the Goal counts the revision."""

    judge: RecordedThreadIncarnation
    text: str

    def change(self, ctx: GoalActionContext) -> Goal:
        goal = ctx.require_goal()
        if goal.state.terminal:
            raise OrderRecordError("A finished order cannot be sent back; issue a new order.")
        snapshot = ctx.goals.registry.snapshot()
        author = goal.author.revised(self.judge, snapshot, time.time())
        return goal.with_text(self.text, ctx.thread, snapshot, author=author)


@dataclass(frozen=True)
class Order:
    """One unit's current order, judged by the commander who issued it."""

    goals: Goals
    unit: Thread
    reference: GoalRef
    goal: Goal
    judge: RecordedThreadIncarnation

    # A verdict reads this many of the unit's newest turns for the judged call.
    judged_turns: ClassVar[int] = 20
    # An accepting reason quotes this many words of the order the call serves.
    serves_words: ClassVar[int] = 4

    @classmethod
    def judged_by(cls, goals: Goals, unit: str, goal_id: str, judge: str) -> Order:
        snapshot = goals.registry.snapshot()
        thread = snapshot.require(unit)
        reference = GoalRef(thread.incarnation.require_recorded(), goal_id)
        goal = reference.current(snapshot)
        if goal is None:
            raise OrderRecordError(f"{unit} does not hold order {goal_id}.")
        commander = snapshot.require(judge).incarnation.require_recorded()
        goal.author.require_commander(commander, snapshot)
        return cls(goals, thread, reference, goal, commander)

    @property
    def records(self) -> OrderRecords:
        return OrderRecords(self.goals.root)

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
        self.records.record(row)
        return row

    def send_back(self, turns: ThreadTurns, tool_call: str, quoted: str, reason: str,
                  text: str) -> Struck:
        row = self._verdict(turns, SentBackVerdict, tool_call, quoted, reason)
        with self.goals.writing() as completed:
            SendBackGoalAction(expect=GoalPrecondition(goal_id=self.reference.goal_id),
                               judge=self.judge, text=text)._apply_locked(
                GoalActionContext(self.goals, self.unit, RuntimeInvocable, completed=completed))
            strikes = self.records.record(row)
        return Struck.counted(self.goals, row, strikes)

    def strike(self, struck: str, cause: type[StrikeCause], what_happened: str,
               caught: type[CaughtStage]) -> Struck:
        row = StrikeRow(id=None, goal=self.reference,
                        struck=self.goals.registry.require(struck).incarnation.require_recorded(),
                        cause=cause, what_happened=what_happened, caught=caught,
                        judge=self.judge, at=time.time())
        return Struck.counted(self.goals, row, self.records.record(row))


@dataclass(frozen=True)
class Struck:
    """A recorded strike and the count it brought its unit to."""

    row: VerdictRow | StrikeRow
    strikes: int

    @classmethod
    def counted(cls, goals: Goals, row: VerdictRow | StrikeRow, strikes: int) -> Struck:
        struck = cls(row, strikes)
        if struck.fired:
            # The limit takes the unit's open order away; #934 allows one per unit.
            thread = goals.registry.require(row.counted_against)
            goal = thread.goal
            if goal is not None and not goal.state.terminal:
                goal.author.release(goals, GoalRef(thread.incarnation.require_recorded(), goal.id))
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

    def record(self, row: VerdictRow | StrikeRow) -> int:
        """Insert one row; answer the unit's strike count in the same transaction."""
        with self.writing() as connection:
            row.add(connection)
            return self._count(connection, row.counted_against)
