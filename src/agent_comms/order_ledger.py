"""Order judgments in the existing goal ledger, using its write/readback lifetime."""

from __future__ import annotations

from collections.abc import Callable
from contextlib import closing
from dataclasses import dataclass, field, replace
from itertools import islice
from typing import Annotated, ClassVar

from .declared_family import DeclaredFamily
from .field_codec import projected
from .goal_attempt_identity import GoalRef
from .goal_failure_observation import GoalLedgerTable
from .plain_text import PlainText
from .thread_identity import RecordedThreadIncarnation
from .typed_table import Column, Index, SqlValue, TypedTable, sql_literal


class OrderRecordError(ValueError):
    """The verdict or strike does not match the order or the unit's work."""


class VerdictOutcome(DeclaredFamily, affix="Verdict"):
    """What the commander decided about one tool call."""

    strikes: ClassVar[bool]

    @classmethod
    def striking(cls) -> tuple[type[VerdictOutcome], ...]:
        return tuple(member for member in cls.members_with(cls) if member.strikes)


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
class VerdictRow(GoalLedgerTable, TypedTable):
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

    def add(self, connection) -> VerdictRow:
        if self.insert_if_absent(connection).rowcount == 0:
            raise OrderRecordError(f"Tool call {self.tool_call} already has a verdict on this order.")
        return self

    def readback(self, connection) -> VerdictRow | None:
        return type(self).one(connection, goal=self.goal, tool_call=self.tool_call)

    @projected(view="wire")
    def line(self) -> str:
        return (f"{self.goal.owner.name}: tool call {self.tool_call} {self.outcome.declared_name} "
                f"by {self.judge.name}: {self.reason}")


@dataclass(frozen=True)
class StrikeRow(GoalLedgerTable, TypedTable):
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

    def add(self, connection) -> StrikeRow:
        return replace(self, id=self.insert(connection).lastrowid)

    def readback(self, connection) -> StrikeRow | None:
        return type(self).one(connection, id=self.id)

    @projected(view="wire")
    def line(self) -> str:
        return (f"{self.struck.name}: {self.cause.declared_name}, caught at "
                f"{self.caught.declared_name} by {self.judge.name}: {self.what_happened}")


class OrderRecords:
    """GoalAttemptStore's judgment capability; never another database or store."""

    strike_limit: ClassVar[int] = 2

    @staticmethod
    def _count(connection, unit: str) -> int:
        striking = ", ".join(map(sql_literal, VerdictOutcome.striking()))
        [count] = SqlValue.for_annotation(int).read(connection.execute(
            f'SELECT (SELECT COUNT(*) FROM "{VerdictRow.declared_name}" '
            f'WHERE "unit" = ? AND "outcome" IN ({striking})) '
            f'+ (SELECT COUNT(*) FROM "{StrikeRow.declared_name}" WHERE "unit" = ?)',
            (unit, unit)))
        return count

    def _rows[Row: TypedTable](self, row: type[Row], **select) -> tuple[Row, ...]:
        with closing(self._connect()) as connection:
            return tuple(row.select(connection, **select))

    def verdicts(self, order: GoalRef) -> tuple[VerdictRow, ...]:
        return self._rows(VerdictRow, goal=order, order_by=("at",))

    def strikes_on(self, order: GoalRef) -> tuple[StrikeRow, ...]:
        return self._rows(StrikeRow, goal=order, order_by=("at",))

    def _newest[Row: TypedTable](self, row: type[Row], limit: int, **select) -> tuple[Row, ...]:
        with closing(self._connect()) as connection, closing(row.stream(
                connection, order_by=("at",), descending=True, **select)) as rows:
            return tuple(islice(rows, limit))

    def recent_strikes(self, limit: int) -> tuple[VerdictRow | StrikeRow, ...]:
        striking = VerdictOutcome.striking()
        rows = (*self._newest(VerdictRow, limit,
                              where=f'"outcome" IN ({", ".join(map(sql_literal, striking))})'),
                *self._newest(StrikeRow, limit))
        return tuple(sorted(rows, key=lambda row: row.at, reverse=True)[:limit])

    def record[Row: VerdictRow | StrikeRow](
        self, row: Row, *, change: Callable[[], object] = lambda: None
    ) -> tuple[Row, int]:
        def write(connection):
            written = row.add(connection)
            change()
            return written, self._count(connection, row.counted_against)

        def verified(result):
            written, count = result
            with closing(self._connect()) as connection:
                return (written.readback(connection) == written
                        and self._count(connection, written.counted_against) == count)

        return self._change(write, verified)
