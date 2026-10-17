"""Commander workflows over Goal history, original transcripts and the goal ledger."""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, ClassVar

from .declared_family import DeclaredFamily
from .field_codec import FieldCodec, projected
from .goal_actions import EditGoalAction, GoalActionContext, GoalPrecondition, RuntimeInvocable
from .goal_attempt_identity import GoalRef
from .goal_history import GoalHistoryEntry
from .order_ledger import (
    AcceptedVerdict, CaughtStage, OrderRecordError, OrderRecords, SentBackVerdict,
    StrikeCause, StrikeRow, VerdictOutcome, VerdictRow,
)
from .thread_identity import RecordedThreadIncarnation
from .transcript_events import ToolStartTranscript

if TYPE_CHECKING:
    from .goal_attempts import GoalAttemptStore
    from .goal_management import Goals
    from .goals import Goal
    from .threads import Thread
    from .turn_digests import ThreadTurns


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
    judged_turns: ClassVar[int] = 20

    @classmethod
    def held(cls, goals: Goals, unit: str, goal_id: str) -> HeldOrder:
        thread = goals.registry.require(unit)
        return cls(goals, thread, GoalRef(thread.incarnation.require_recorded(), goal_id))

    @property
    def records(self) -> GoalAttemptStore:
        return self.goals.open_goal_store()

    @property
    def history(self) -> tuple[GoalHistoryEntry, ...]:
        return self.goals.goal_history(self.unit.name, goal_id=self.reference.goal_id)

    def work(self, turns: ThreadTurns) -> tuple[OrderCall, ...]:
        """Original calls since issue, newest first, with their recorded verdicts."""
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
    """Read original Goal transitions and the judgment rows, never a stored story."""

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
            row, _ = self.records.record(row)
        return row

    def send_back(self, turns: ThreadTurns, tool_call: str, quoted: str, reason: str,
                  text: str) -> Struck:
        row = self._verdict(turns, SentBackVerdict, tool_call, quoted, reason)
        with self.goals.writing() as completed:
            unit = self._require_current()
            action = SendBackGoalAction(expect=GoalPrecondition(expected_goal=self.goal),
                                        judge=self.judge, text=text)
            context = GoalActionContext(self.goals, unit, RuntimeInvocable, completed=completed)
            row, strikes = self.records.record(row, change=lambda: action._apply_locked(context))
        return Struck.counted(self.goals, row, strikes)

    def strike(self, struck: str, cause: type[StrikeCause], what_happened: str,
               caught: type[CaughtStage]) -> Struck:
        with self.goals.writing():
            self._require_current()
            row = StrikeRow(id=None, goal=self.reference,
                            struck=self.goals.registry.require(struck).incarnation.require_recorded(),
                            cause=cause, what_happened=what_happened, caught=caught,
                            judge=self.judge, at=time.time())
            row, strikes = self.records.record(row)
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
            for order in goals.registry.require(row.counted_against).open_orders:
                order.require_current(goals.registry).author.release(goals, order)
        return struck

    @projected(view="wire")
    def fired(self) -> bool:
        return self.strikes >= OrderRecords.strike_limit
