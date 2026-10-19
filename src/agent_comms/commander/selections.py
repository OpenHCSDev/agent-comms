"""Command-boundary selections; absence has an answer, not a nullable value."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any

from ..change_report import FileLine
from ..goal_actions import OrderGoalAction
from ..thread_identity import ThreadIncarnation
from ..todos import Assignment, WithdrawTodo


class AssignmentTrace(ABC):
    @classmethod
    def read(cls, text: str) -> AssignmentTrace:
        return TracedAssignment(FileLine.from_wire(text)) if text else UntracedAssignment()

    @abstractmethod
    def order(self, **arguments: Any) -> OrderGoalAction: ...


@dataclass(frozen=True)
class UntracedAssignment(AssignmentTrace):
    def order(self, **arguments: Any) -> OrderGoalAction:
        return OrderGoalAction(**arguments)


@dataclass(frozen=True)
class TracedAssignment(AssignmentTrace):
    where: FileLine

    def order(self, **arguments: Any) -> OrderGoalAction:
        return OrderGoalAction(traced=self.where, **arguments)


class TransferReason(ABC):
    @classmethod
    def read(cls, text: str) -> TransferReason:
        return ChangedAssignee(text) if text else SameAssignee()

    @abstractmethod
    def errors(self) -> tuple[str, ...]: ...


@dataclass(frozen=True)
class SameAssignee(TransferReason):
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
                "--reassign needs the concrete failure that forces replacement (8+ words), told to the old and new owner",
            )
        )


class StrikeSelection(ABC):
    @classmethod
    def read(cls, text: str) -> StrikeSelection:
        return StrikeAt(text) if text else LatestStrike()

    @abstractmethod
    def includes(self, clock: str) -> bool: ...


@dataclass(frozen=True)
class LatestStrike(StrikeSelection):
    def includes(self, clock: str) -> bool:
        return True


@dataclass(frozen=True)
class StrikeAt(StrikeSelection):
    clock: str

    def includes(self, clock: str) -> bool:
        return self.clock == clock


class WithdrawalSelection(ABC):
    @classmethod
    def read(cls, text: str) -> WithdrawalSelection:
        return SupersededOrder(text) if text else MistakenOrder()

    @abstractmethod
    def operation(
        self, revision: int, actor: ThreadIncarnation, previous: Assignment, why: str
    ) -> WithdrawTodo: ...


@dataclass(frozen=True)
class MistakenOrder(WithdrawalSelection):
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
