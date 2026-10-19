"""The commander-native plan's phases: each declares the phases it waits for.

This family is the one owner of the plan's order and dependencies;
``plans/commander-native-plan.md`` points here. An order names the phase it
serves in ``ProblemOrder.serves``; an order outside the plan serves
``UnplannedPhase``. A phase is done when every order serving it is closed.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import TYPE_CHECKING, ClassVar

from .declared_family import DeclaredFamily

if TYPE_CHECKING:
    from .todos import Todo


class OrderPhase(DeclaredFamily, affix="Phase"):
    """What an order serves: one plan phase, or nothing in the plan."""


class UnplannedPhase(OrderPhase):
    pass


class PlanPhase(OrderPhase, declared_name=None):
    after: ClassVar[tuple[type[PlanPhase], ...]] = ()

    @classmethod
    def serving(cls, todos: Iterable[Todo]) -> tuple[Todo, ...]:
        return tuple(todo for todo in todos if todo.order.serves is cls)

    @classmethod
    def done(cls, todos: Iterable[Todo]) -> bool:
        serving = cls.serving(todos)
        return bool(serving) and not any(todo.state.open for todo in serving)

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


class ReviewQueuePhase(PlanPhase):
    after = (RecordPlanePhase,)


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
