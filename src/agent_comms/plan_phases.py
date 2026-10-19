"""The commander-native plan's phases: each declares the phases it waits for.

This family is the one owner of the plan's order and dependencies;
``plans/commander-native-plan.md`` points here. An order names the phase it
serves in ``ProblemOrder.serves``; an order outside the plan serves
``UnplannedPhase``. A phase is done only when its declared full-phase task is accepted; closing
related subtasks does not establish completion of the phase.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import TYPE_CHECKING, ClassVar

from .declared_family import DeclaredFamily
from .work_dispatch import UnspecifiedPhaseTask

if TYPE_CHECKING:
    from .todos import Todo
    from .work_dispatch import PhaseTask


class OrderPhase(DeclaredFamily, affix="Phase"):
    """What an order serves: one plan phase, or nothing in the plan."""


class UnplannedPhase(OrderPhase):
    pass


class PlanPhase(OrderPhase, declared_name=None):
    after: ClassVar[tuple[type[PlanPhase], ...]] = ()
    task: ClassVar[PhaseTask] = UnspecifiedPhaseTask()

    @classmethod
    def serving(cls, todos: Iterable[Todo]) -> tuple[Todo, ...]:
        return tuple(todo for todo in todos if todo.order.serves is cls)

    @classmethod
    def done(cls, todos: Iterable[Todo]) -> bool:
        return cls.task.done(cls.serving(todos))

    @classmethod
    def ordered(cls, todos: Iterable[Todo]) -> bool:
        return any(todo.state.open and todo.holder for todo in cls.serving(todos))

    @classmethod
    def ready(cls, todos: Iterable[Todo]) -> bool:
        todos = tuple(todos)
        return all(phase.done(todos) for phase in cls.after) and not cls.done(todos)


class MergedPhase(PlanPhase, declared_name=None):
    """A phase delivered before orders named the phase they serve; ``delivered`` is its record."""

    delivered: ClassVar[str]

    @classmethod
    def done(cls, todos: Iterable[Todo]) -> bool:
        return True


class StopTheSplitPhase(MergedPhase):
    delivered = "no code: branches deleted and the scratch copy frozen on 2026-10-18"


class RetainedStorePhase(MergedPhase):
    delivered = "a75deaf04"


class OneOrderOwnerPhase(MergedPhase):
    delivered = "9293095d9"


class DesignReconciledPhase(MergedPhase):
    after = (OneOrderOwnerPhase,)
    delivered = "64fbd8bc1"


class RecordPlanePhase(PlanPhase):
    after = (RetainedStorePhase,)


class ReviewQueuePhase(MergedPhase):
    after = (RecordPlanePhase,)
    delivered = "41e4ec7319432755d75dcd8dbbab35839a4eb005 (#1654)"


class ControlPlanePhase(PlanPhase):
    after = (RecordPlanePhase,)


class CutoverPhase(PlanPhase):
    # The parity gate in the Markdown plan has no executable declaration yet.
    # These dependencies do not establish parity or authorize deleting scripts.
    after = (RecordPlanePhase, ReviewQueuePhase, ControlPlanePhase)


class StoryPhase(PlanPhase):
    """A view of the native records, not a prerequisite for their cutover."""

    after = (RecordPlanePhase,)


class CommanderPagePhase(PlanPhase):
    """A view of the native records and control plane, independent of cutover."""

    after = (RecordPlanePhase, ControlPlanePhase)


class SkillPhase(PlanPhase):
    after = (CutoverPhase,)
