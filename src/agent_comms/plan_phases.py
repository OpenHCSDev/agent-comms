"""The owner's plans and their phases: each phase declares its plan, the phases it waits
for, and the evidence on main that proves it done.

This family is the one owner of every plan's order and dependencies; each plan document
points here. An order names the phase it serves through ``ProblemOrder.phase``; an order
outside every plan serves ``UnplannedPhase``. A phase is done only when its declared
evidence holds on the plan repository's ``origin/main``; accepting a task proves nothing.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar

from agent_comms.declared_family import DeclaredFamily
from agent_comms.phase_evidence import (
    AllEvidence,
    CommitMerged,
    NoDeclaredEvidence,
    ParityEvidence,
    PathsAbsent,
    PatternAbsent,
    PatternPresent,
    PhaseEvidence,
    PlanReading,
)
from agent_comms.phase_tasks import PhaseTask, UnspecifiedPhaseTask

if TYPE_CHECKING:
    from agent_comms.todos import Todo


class Plan(DeclaredFamily, affix="Plan"):
    """A plan the owner directs; ``document`` is its prose, which points back here."""

    document: ClassVar[str]


class CommanderNativePlan(Plan):
    document = "plans/commander-native-plan.md"


class VirtualBusPlan(Plan):
    document = "plans/VIRTUAL-BUS-PLAN.md"


class MultipleInheritancePlan(Plan):
    document = "plans/MULTIPLE-INHERITANCE-PLAN.md"


class RemainingSlopPlan(Plan):
    document = "plans/REMAINING-SLOP-PLAN.md"


class OrderPhase(DeclaredFamily, affix="Phase"):
    """What an order serves: one plan phase, or nothing in any plan."""

    @classmethod
    def order_refusals(cls, reading: PlanReading, reason: str) -> tuple[str, ...]:
        return ()


class UnplannedPhase(OrderPhase):
    @classmethod
    def order_refusals(cls, reading: PlanReading, reason: str) -> tuple[str, ...]:
        ready = tuple(phase for phase in PlanPhase.members_with(PlanPhase) if phase.ready(reading))
        if ready and len(reason.split()) < 8:
            return (
                "Plan phases are ready: "
                + ", ".join(phase.declared_name for phase in ready)
                + ". Name a phase, or provide an explicit unplanned_reason of 8+ words.",
            )
        return ()


class PlanPhase(OrderPhase, declared_name=None):
    plan: ClassVar[type[Plan]]
    after: ClassVar[tuple[type[PlanPhase], ...]] = ()
    evidence: ClassVar[PhaseEvidence] = NoDeclaredEvidence()
    task: ClassVar[PhaseTask] = UnspecifiedPhaseTask()

    @classmethod
    def serving(cls, todos: tuple[Todo, ...]) -> tuple[Todo, ...]:
        return tuple(todo for todo in todos if todo.order.phase is cls)

    @classmethod
    def done(cls, reading: PlanReading) -> bool:
        return reading.holds(cls.evidence)

    @classmethod
    def ordered(cls, reading: PlanReading) -> bool:
        return any(todo.state.open and todo.holder for todo in cls.serving(reading.todos))

    @classmethod
    def ready(cls, reading: PlanReading) -> bool:
        return all(phase.done(reading) for phase in cls.after) and not cls.done(reading)


class CommanderNativePhase(PlanPhase, declared_name=None):
    plan = CommanderNativePlan


class VirtualBusPhase(PlanPhase, declared_name=None):
    plan = VirtualBusPlan


class MultipleInheritancePhase(PlanPhase, declared_name=None):
    plan = MultipleInheritancePlan


class RemainingSlopPhase(PlanPhase, declared_name=None):
    plan = RemainingSlopPlan


class RetainedStorePhase(CommanderNativePhase):
    evidence = CommitMerged("a75deaf04")


class OneOrderOwnerPhase(CommanderNativePhase):
    evidence = CommitMerged("9293095d9")


class DesignReconciledPhase(CommanderNativePhase):
    after = (OneOrderOwnerPhase,)
    evidence = CommitMerged("64fbd8bc1")


class RecordPlanePhase(CommanderNativePhase):
    after = (RetainedStorePhase,)
    evidence = CommitMerged("465a27988")


class ReviewQueuePhase(CommanderNativePhase):
    """Revised 2026-10-21: a verdict per PR head gates the merge queue, so the review-queue
    commit no longer proves this phase; P433 declares what main must show."""

    after = (RecordPlanePhase,)


class ControlPlanePhase(CommanderNativePhase):
    after = (RecordPlanePhase,)


class ParityPhase(CommanderNativePhase):
    after = (RecordPlanePhase, ReviewQueuePhase, ControlPlanePhase)
    evidence = ParityEvidence()


class CutoverPhase(CommanderNativePhase):
    after = (RecordPlanePhase, ReviewQueuePhase, ControlPlanePhase, ParityPhase)


class StoryPhase(CommanderNativePhase):
    """A view of the native records, not a prerequisite for their cutover."""

    after = (RecordPlanePhase,)


class CommanderPagePhase(CommanderNativePhase):
    """A view of the native records and control plane, independent of cutover."""

    after = (RecordPlanePhase, ControlPlanePhase)


class SkillPhase(CommanderNativePhase):
    after = (CutoverPhase,)


OLD_LOG_READ = r"\bWireLog\b|wire_log"


class BusInterfacePhase(VirtualBusPhase):
    evidence = CommitMerged("4792954e")


class BusWriterPhase(VirtualBusPhase):
    """W: the one writer onto ``Bus.append``."""

    after = (BusInterfacePhase,)
    evidence = PatternAbsent(
        "append_private_unlocked",
        ("src/agent_comms/bus_publication.py", "src/agent_comms/publisher.py"),
    )


class BusDeliveryReadersPhase(VirtualBusPhase):
    """B1: delivery, wake, cursor and notification readers on ``seq``.

    ``store_order`` keeps ``WireLog`` until C deletes the class, so it is not read here."""

    after = (BusWriterPhase,)
    evidence = PatternAbsent(
        OLD_LOG_READ,
        (
            "src/agent_comms/wake_candidate_index.py",
            "src/agent_comms/notification_assignment.py",
            "src/agent_comms/read_ledger.py",
            "src/agent_comms/cursor_publication.py",
            "src/agent_comms/goal_waits.py",
        ),
    )


class BusHistoryReadersPhase(VirtualBusPhase):
    """B2: history, presentation and transcript readers on ``seq``."""

    after = (BusWriterPhase,)
    evidence = PatternAbsent(
        OLD_LOG_READ,
        (
            "src/agent_comms/historical_views.py",
            "src/agent_comms/presentation.py",
            "src/agent_comms/transcripts.py",
            "src/agent_comms/transcript_sources.py",
            "src/agent_comms/active_route.py",
            "src/agent_comms/acp.py",
            "src/agent_comms/comms.py",
            "src/agent_comms/message_bus.py",
            "scripts/attach_historical_views.py",
        ),
    )


class BusCarryPhase(VirtualBusPhase):
    """D: the one carry of the committed history into the bus database."""

    after = (BusWriterPhase,)
    evidence = PatternPresent(r"bus\.sqlite3|Bus\.path_in", ("tools/cutover",))


class BusWatchedPhase(VirtualBusPhase):
    """E: ``Watched`` replaces file following."""

    after = (BusWriterPhase,)
    evidence = PathsAbsent(("src/agent_comms/wire_watch.py",))


class BusActivityPhase(VirtualBusPhase):
    evidence = PathsAbsent(("src/agent_comms/activity_checkpoint.py",))


class BusDeletionPhase(VirtualBusPhase):
    """C: everything under the plan's "Deleted, not ported"."""

    after = (BusDeliveryReadersPhase, BusHistoryReadersPhase, BusCarryPhase)
    evidence = AllEvidence(
        (
            PathsAbsent(
                (
                    "src/agent_comms/private_bus_checkpoint.py",
                    "src/agent_comms/checkpoint_seals.py",
                    "src/agent_comms/bus_page_index.py",
                    "src/agent_comms/wire_log.py",
                    "src/agent_comms/wire_record.py",
                )
            ),
            PatternAbsent(r"bus\.jsonl|bus_meta|_wire_lock_path|has_private_wire_fields"),
        )
    )


FLAG = r"(: ClassVar\[bool\])? = (True|False)$"


class LifecycleFlagsPhase(MultipleInheritancePhase):
    """1A."""

    evidence = PatternAbsent(
        r"^\s+(terminal|failed|successful|pending|active|executing"
        r"|unresolved|deferred|complete|completed)" + FLAG
    )


class CodecFlagsPhase(MultipleInheritancePhase):
    """1B."""

    evidence = PatternAbsent(
        r"^\s+(accepts_null|opaque|external_json_shape|strict_fields|accepts_prompt)" + FLAG
    )


class SmallFamilyFlagsPhase(MultipleInheritancePhase):
    """1C. ``GoalDisplay.visible`` stays by the commander's decision, so ``visible`` is not read."""

    evidence = PatternAbsent(
        r"^\s+(multiple_targets|preengagement_target|notification_changed|input_uncertain)" + FLAG
    )


class TriggersPhase(MultipleInheritancePhase):
    """1D: every trigger derives from its table's capability in ``typed_table.py``."""

    evidence = PatternAbsent(
        r"BEFORE (UPDATE|DELETE|INSERT)", ("src", ":!src/agent_comms/typed_table.py")
    )


class DuplicatesPhase(MultipleInheritancePhase):
    """1E."""


class FieldDirectivesPhase(MultipleInheritancePhase):
    """Batch 2."""

    after = (
        LifecycleFlagsPhase,
        CodecFlagsPhase,
        SmallFamilyFlagsPhase,
        TriggersPhase,
        DuplicatesPhase,
    )


class OwnTypeChecksPhase(MultipleInheritancePhase):
    """Batch 3."""

    after = (FieldDirectivesPhase,)


class InheritanceCleanupPhase(MultipleInheritancePhase):
    """Batch 4."""

    after = (OwnTypeChecksPhase,)


class DeadModulesPhase(RemainingSlopPhase):
    """0.3."""

    evidence = PathsAbsent(("src/agent_comms/acp_ingress.py",))


class TransitionScaffoldingPhase(RemainingSlopPhase):
    """0.4."""

    after = (CutoverPhase,)
    evidence = PathsAbsent(
        (
            "src/agent_comms/commander_parity",
            "tools/carry_rts_orders.py",
            "tools/cutover/rts-launchers.patch",
        )
    )


class AbsencePhase(RemainingSlopPhase):
    """2.1."""


class TypeSwitchesPhase(RemainingSlopPhase):
    """2.4: the AST's own visitor replaces switches on its node types."""

    evidence = PatternAbsent(r"isinstance\([^)]*ast\.", ("src/agent_comms/commander/order.py",))


class OrderSendPhase(RemainingSlopPhase):
    """2.5."""


class ExternalBoundariesPhase(RemainingSlopPhase):
    """Wave 3."""

    evidence = PatternAbsent(r"^\s*except Exception\b")
