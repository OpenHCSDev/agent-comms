"""The commander-native plan's phases: each declares the phases it waits for.

This family is the one owner of the plan's order and dependencies;
``plans/commander-native-plan.md`` points here. An order names the phase it
serves through ``ProblemOrder.phase``; an order outside the plan serves
``UnplannedPhase``. A phase is done only when its declared full-phase task is accepted; closing
related subtasks does not establish completion of the phase.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import TYPE_CHECKING, ClassVar

from .commander_parity import ParityRow
from .declared_family import DeclaredFamily
from .phase_tasks import PhaseTask, UnspecifiedPhaseTask

if TYPE_CHECKING:
    from .todos import Todo


class OrderPhase(DeclaredFamily, affix="Phase"):
    """What an order serves: one plan phase, or nothing in the plan."""

    @classmethod
    def order_refusals(cls, todos: Iterable[Todo], reason: str) -> tuple[str, ...]:
        return ()


class UnplannedPhase(OrderPhase):
    @classmethod
    def order_refusals(cls, todos: Iterable[Todo], reason: str) -> tuple[str, ...]:
        rows = tuple(todos)
        ready = tuple(phase for phase in PlanPhase.members_with(PlanPhase) if phase.ready(rows))
        if ready and len(reason.split()) < 8:
            return ('Plan phases are ready: ' + ', '.join(phase.declared_name for phase in ready)
                    + '. Name a phase, or declare Serves: unplanned with a reason of 8+ words.',)
        return ()


class PlanPhase(OrderPhase, declared_name=None):
    after: ClassVar[tuple[type[PlanPhase], ...]] = ()
    task: ClassVar[PhaseTask] = UnspecifiedPhaseTask()

    @classmethod
    def serving(cls, todos: Iterable[Todo]) -> tuple[Todo, ...]:
        return tuple(todo for todo in todos if todo.order.phase is cls)

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


class ParityPhase(PlanPhase):
    after = (RecordPlanePhase, ReviewQueuePhase, ControlPlanePhase)

    @classmethod
    def done(cls, todos: Iterable[Todo]) -> bool:
        return ParityRow.done(todos)


class CutoverPhase(PlanPhase):
    after = (RecordPlanePhase, ReviewQueuePhase, ControlPlanePhase, ParityPhase)


class StoryPhase(PlanPhase):
    """A view of the native records, not a prerequisite for their cutover."""

    after = (RecordPlanePhase,)


class CommanderPagePhase(PlanPhase):
    """A view of the native records and control plane, independent of cutover."""

    after = (RecordPlanePhase, ControlPlanePhase)


class SkillPhase(PlanPhase):
    after = (CutoverPhase,)
