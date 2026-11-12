from __future__ import annotations

from typing import ClassVar

from agent_comms.nominal.declared_family import DeclaredFamily
from agent_comms.planning.order_phase import OrderPhase
from agent_comms.planning.orders import Todo
from agent_comms.planning.phase_evidence import PhaseEvidence
from agent_comms.planning.phase_tasks import PhaseTask, UnspecifiedPhaseTask
from agent_comms.planning.plan_reading import PlanReading


class Plan(DeclaredFamily, affix="Plan"):
    """A plan the owner directs; ``document`` is its prose, which points back here."""

    document: ClassVar[str]


class TeamNativePlan(Plan, declared_name="team_native"):
    document = "plans/team-native-plan.md"


class VirtualBusPlan(Plan):
    document = "plans/VIRTUAL-BUS-PLAN.md"


class MultipleInheritancePlan(Plan):
    document = "plans/MULTIPLE-INHERITANCE-PLAN.md"


class RemainingSlopPlan(Plan):
    document = "plans/REMAINING-SLOP-PLAN.md"


class Id128NominalPlan(Plan):
    document = "plans/ID128-NOMINAL-PLAN.md"


class StoreGuardsPlan(Plan):
    document = "plans/STORE-GUARDS-PLAN.md"


class OwnerAsksPlan(Plan):
    document = "plans/OWNER-ASKS-PLAN.md"


class PackageStructurePlan(Plan):
    document = "plans/PACKAGE-STRUCTURE-PLAN.md"


class ModuleOwnershipPlan(Plan):
    """The measured move: which package owns each flat module, proved by consumers, not paths."""

    document: ClassVar[str] = "plans/MODULE-OWNERSHIP-PLAN.md"
    evidence_manifest: ClassVar[str] = "docs/plan-evidence/module-map-manifest.json"


class PlanPhase(OrderPhase, declared_name=None):
    """Every named phase declares its plan and the evidence that proves it done; neither has
    a default, because a phase whose work could never clear it is not a phase."""

    plan: ClassVar[type[Plan]]
    after: ClassVar[tuple[type[PlanPhase], ...]] = ()
    evidence: ClassVar[PhaseEvidence]
    task: ClassVar[PhaseTask] = UnspecifiedPhaseTask()

    @classmethod
    def phase_evidence(cls) -> PhaseEvidence:
        return cls.evidence

    @classmethod
    def serving(cls, todos: tuple[Todo, ...]) -> tuple[Todo, ...]:
        return tuple(todo for todo in todos if todo.order.phase is cls)

    @classmethod
    def done(cls, reading: PlanReading) -> bool:
        return reading.holds(cls.phase_evidence())

    @classmethod
    def holding(cls, reading: PlanReading) -> tuple[Todo, ...]:
        """The open orders serving this phase that a unit holds."""
        return tuple(todo for todo in cls.serving(reading.todos) if todo.state.is_open() and todo.holder)

    @classmethod
    def ordered(cls, reading: PlanReading) -> bool:
        return bool(cls.holding(reading))

    @classmethod
    def ordered_since(cls, reading: PlanReading) -> float:
        """When the phase was first ordered to its current holders; a re-sent order keeps the
        time of the first, so re-sending never restarts the clock."""
        return min(todo.first_ordered_at for todo in cls.holding(reading))

    @classmethod
    def ready(cls, reading: PlanReading) -> bool:
        return all(phase.done(reading) for phase in cls.after) and not cls.done(reading)
