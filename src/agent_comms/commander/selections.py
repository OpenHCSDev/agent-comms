"""Command-boundary selections; absence has an answer, not a nullable value."""

from __future__ import annotations

from dataclasses import dataclass, replace

from ..change_report import FileLine
from ..goal_actions import OrderGoalAction
from ..thread_identity import ThreadIncarnation
from ..todos import Assignment, WithdrawTodo


class AssignmentTrace:
    @classmethod
    def read(cls, text: str) -> AssignmentTrace:
        return TracedAssignment(FileLine.from_wire(text)) if text else cls()

    def order(self, action: OrderGoalAction) -> OrderGoalAction:
        return action


@dataclass(frozen=True)
class TracedAssignment(AssignmentTrace):
    where: FileLine

    def order(self, action: OrderGoalAction) -> OrderGoalAction:
        return replace(action, traced=self.where)


class TransferReason:
    @classmethod
    def read(cls, text: str) -> TransferReason:
        return ChangedAssignee(text) if text else cls()

    def errors(self) -> tuple[str, ...]:
        return ()


@dataclass(frozen=True)
class ChangedAssignee(TransferReason):
    reason: str

    def errors(self) -> tuple[str, ...]:
        return (
            ()
            if len(self.reason.split()) >= 8
            else (
                "--reassign needs the concrete failure that forces replacement (8+ words), "
                "told to the old and new owner",
            )
        )


class StrikeSelection:
    @classmethod
    def read(cls, text: str) -> StrikeSelection:
        return StrikeAt(text) if text else cls()

    def includes(self, clock: str) -> bool:
        return True


@dataclass(frozen=True)
class StrikeAt(StrikeSelection):
    clock: str

    def includes(self, clock: str) -> bool:
        return self.clock == clock


class WithdrawalSelection:
    @classmethod
    def read(cls, text: str) -> WithdrawalSelection:
        return SupersededOrder(text) if text else cls()

    def operation(
        self, revision: int, actor: ThreadIncarnation, previous: Assignment, why: str
    ) -> WithdrawTodo:
        return WithdrawTodo(revision, actor, previous, why)


@dataclass(frozen=True)
class SupersededOrder(WithdrawalSelection):
    problem: str

    def operation(
        self, revision: int, actor: ThreadIncarnation, previous: Assignment, why: str
    ) -> WithdrawTodo:
        return WithdrawTodo(revision, actor, previous, why, superseded_by=self.problem)
