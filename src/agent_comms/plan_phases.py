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
from agent_comms.field_codec import Id128
from agent_comms.phase_evidence import (
    AllEvidence,
    BusStorageAbsent,
    ClassFlagsAbsent,
    QualifiedName,
    ClassDeclared,
    ClassRostersAbsent,
    CommitMerged,
    FunctionDeclared,
    NoAbsenceProbes,
    NoExactCopies,
    NoGodFunction,
    OwnClassChecksAbsent,
    ParentsShared,
    ParityEvidence,
    PathsAbsent,
    PatternAbsent,
    PatternPresent,
    PhaseEvidence,
    PlanReading,
    RestatedMembersAbsent,
    RulesetGates,
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


class Id128NominalPlan(Plan):
    document = "plans/ID128-NOMINAL-PLAN.md"


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
    """Every named phase declares its plan and the evidence that proves it done; neither has
    a default, because a phase whose work could never clear it is not a phase."""

    plan: ClassVar[type[Plan]]
    after: ClassVar[tuple[type[PlanPhase], ...]] = ()
    evidence: ClassVar[PhaseEvidence]
    task: ClassVar[PhaseTask] = UnspecifiedPhaseTask()

    @classmethod
    def serving(cls, todos: tuple[Todo, ...]) -> tuple[Todo, ...]:
        return tuple(todo for todo in todos if todo.order.phase is cls)

    @classmethod
    def done(cls, reading: PlanReading) -> bool:
        return reading.holds(cls.evidence)

    @classmethod
    def holding(cls, reading: PlanReading) -> tuple[Todo, ...]:
        """The open orders serving this phase that a unit holds."""
        return tuple(todo for todo in cls.serving(reading.todos) if todo.state.open and todo.holder)

    @classmethod
    def ordered(cls, reading: PlanReading) -> bool:
        return bool(cls.holding(reading))

    @classmethod
    def ordered_since(cls, reading: PlanReading) -> float:
        """When the phase was first ordered to its current holders; a re-sent order keeps the
        time of the first, so re-sending never restarts the clock."""
        return min(
            order.at.recorded_at("A plan order")
            for todo in cls.holding(reading)
            for order in (*todo.prior_orders, todo.order.checked())
        )

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


class Id128NominalPhase(PlanPhase, declared_name=None):
    plan = Id128NominalPlan
    paths: ClassVar[tuple[str, ...]] = ("src", ":!src/agent_comms/skills/")


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
    """A verdict per PR head gates merging through main's required review status.

    No GitHub merge queue is required. A bypass actor defeats the review gate.
    """

    after = (RecordPlanePhase,)
    evidence = AllEvidence((
        ClassDeclared("HeadReviewRecord", ("src/agent_comms/todos.py",)),
        RulesetGates(),
    ))


COMMANDER = "src/agent_comms/commander/"


class ControlPlanePhase(CommanderNativePhase):
    """Phase 7's deletes: Core owns every signal these scripts raised."""

    after = (RecordPlanePhase,)
    evidence = PathsAbsent(
        tuple(
            COMMANDER + name
            for name in (
                "rotate.py",
                "capacity.py",
                "forks",
                "growth_watch.py",
                "growth_seen.json",
                "storage.py",
                "rules.py",
                "rules.jsonl",
            )
        )
    )


class StoryPhase(CommanderNativePhase):
    """A view of the native records, not a prerequisite for their cutover: both story tools
    exist and the separate roast step is deleted."""

    after = (RecordPlanePhase,)
    evidence = AllEvidence(
        (
            ClassDeclared("CommsOrderStoryTool", ("src/agent_comms/order_tools.py",)),
            ClassDeclared("CommsFleetStoryTool", ("src/agent_comms/order_tools.py",)),
            PatternAbsent(r"^def roast\b", (COMMANDER + "judge.py",)),
        )
    )


class CommanderPagePhase(CommanderNativePhase):
    """A view of the native records and control plane, independent of cutover."""

    after = (RecordPlanePhase, ControlPlanePhase)
    evidence = AllEvidence(
        (
            ClassDeclared("CommanderHud", ("src",)),
            PatternPresent(r"\bcomms_hud\b", ("src",)),
        )
    )


class ParityPhase(CommanderNativePhase):
    """The native capability owners, not a second matrix of demonstrations, decide completion."""

    after = (RecordPlanePhase, ReviewQueuePhase, ControlPlanePhase, CommanderPagePhase)
    evidence = ParityEvidence(tuple(phase.evidence for phase in after))


class CutoverPhase(CommanderNativePhase):
    """The native evidence passes and the machine-wide commander configuration is gone."""

    after = (ParityPhase,)
    evidence = AllEvidence((ParityPhase.evidence, PatternAbsent(r"\bCommanderConfig\b")))


class SkillPhase(CommanderNativePhase):
    """The skill names no script."""

    after = (CutoverPhase,)
    evidence = PatternAbsent(r"agent-comms-commander", ("src/agent_comms/skills/comms-commander",))


OLD_LOG_READ = r"\bWireLog\b|wire_log"


class BusInterfacePhase(VirtualBusPhase):
    evidence = CommitMerged("4792954e")


class BusWriterPhase(VirtualBusPhase):
    """W: the one writer onto ``Bus.append``."""

    after = (BusInterfacePhase,)
    evidence = PatternAbsent(
        "[a]ppend_private_unlocked",
        ("src/agent_comms/bus_publication.py", "src/agent_comms/publisher.py"),
    )


class BusDeliveryReadersPhase(VirtualBusPhase):
    """B1: delivery, wake, cursor and notification readers on ``seq``."""

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
    """D: the one carry of the committed history into the bus database.

    Done once the JSONL history writer the carry read from is gone: the bus database is
    then the only store, and the finished carry is deleted rather than kept as evidence."""

    after = (BusWriterPhase,)
    evidence = PathsAbsent(("src/agent_comms/wire_log.py",))


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
            BusStorageAbsent(),
        )
    )


FLAG = r"(: ClassVar\[bool\])? = (True|False)$"
LIFECYCLE_FLAGS = (
    "terminal|failed|successful|pending|active|executing|unresolved|deferred|complete|completed"
)
CODEC_FLAGS = "accepts_null|opaque|external_json_shape|strict_fields|accepts_prompt"
SMALL_FAMILY_FLAGS = "multiple_targets|preengagement_target|notification_changed|input_uncertain"


class LifecycleFlagsPhase(MultipleInheritancePhase):
    """1A: flags declared on classes; a function's local named ``complete`` is not one."""

    evidence = ClassFlagsAbsent(rf"^\s+({LIFECYCLE_FLAGS})" + FLAG)


class CodecFlagsPhase(MultipleInheritancePhase):
    """1B."""

    evidence = PatternAbsent(rf"^\s+({CODEC_FLAGS})" + FLAG)


class SmallFamilyFlagsPhase(MultipleInheritancePhase):
    """1C. ``GoalDisplay.visible`` stays by the commander's decision, so ``visible`` is not read."""

    evidence = PatternAbsent(rf"^\s+({SMALL_FAMILY_FLAGS})" + FLAG)


class TriggersPhase(MultipleInheritancePhase):
    """1D: every trigger derives from its table's capability in ``typed_table.py``."""

    evidence = PatternAbsent(
        r"BEFORE (UPDATE|DELETE|INSERT)", ("src", ":!src/agent_comms/typed_table.py")
    )


class DuplicatesPhase(MultipleInheritancePhase):
    """1E: the plan's eight exact groups; its three near groups need their readers traced."""

    evidence = AllEvidence(
        tuple(
            NoExactCopies(names, tuple(f"src/agent_comms/{path}" for path in paths))
            for names, paths in (
                (
                    ("__init__",),
                    (
                        "attempt_store.py",
                        "cli_bridges/opencode.py",
                        "cli_bridges/shared.py",
                        "external_transcript.py",
                        "recovery_reader.py",
                    ),
                ),
                (
                    ("captured_owner", "owner_identity"),
                    (
                        "cohort_schema.py",
                        "coordination_tables/attempts.py",
                        "native_input_record.py",
                        "participant_store.py",
                    ),
                ),
                (
                    ("trigger_bodies",),
                    ("coordination_response.py", "native_prompt_binding.py", "private_sidecar.py"),
                ),
                (
                    ("__init__",),
                    ("durable_turn.py", "execution_store.py", "exporting.py", "publisher.py"),
                ),
                (("wire_member",), ("pi_payloads.py", "field_codec.py")),
                (
                    (
                        "_assert_response_schema",
                        "assert_cohort_schema",
                        "assert_native_runtime_schema",
                        "require_schema",
                    ),
                    (
                        "cohort_schema.py",
                        "coordinated_runtime_schema.py",
                        "coordination_response.py",
                        "typed_table.py",
                    ),
                ),
                (
                    ("references",),
                    (
                        "coordination_tables/attempts.py",
                        "coordination_tables/recovery.py",
                        "coordination_tables/responses.py",
                    ),
                ),
                (
                    ("annotation_disclosure", "disclosure_for"),
                    ("pi_payloads.py", "turn_context.py", "annotation_disclosure.py"),
                ),
            )
        )
    )


class FieldDirectivesPhase(MultipleInheritancePhase):
    """Batch 2: the field groups and table mixins the plan names exist and each has members.
    The bus groups are deleted by the virtual bus, and ``Addressed`` is the bus's own.
    SchemaMeta has no shared field beyond SingletonRow.
    Revisioned and ExecutionKeyed would reorder table columns because inherited fields
    come first (typed_table.py:1480), violating the plan's stored DDL rule."""

    evidence = ParentsShared(
        names=(
            "TurnItem",
            "ClaimScoped",
            "SingletonRow",
            "WithoutRowid",
        )
    )

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
    evidence = OwnClassChecksAbsent()


class InheritanceCleanupPhase(MultipleInheritancePhase):
    """Batch 4, the deletion closure: no capability shell left with one member, no member
    restating its base's answer, no probe for a batch 1 flag, and no literal roster of
    classes."""

    after = (OwnTypeChecksPhase,)
    evidence = AllEvidence(
        (
            ParentsShared(paths=("src/agent_comms/lifecycle_capability.py",)),
            RestatedMembersAbsent(),
            PatternAbsent(
                rf"\b(getattr|hasattr)\([^,()]+, *[\"']"
                rf"({LIFECYCLE_FLAGS}|{CODEC_FLAGS}|{SMALL_FAMILY_FLAGS})[\"']"
            ),
            ClassRostersAbsent(families=(PlanPhase,)),
        )
    )


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
    """2.1: the seven heaviest files first, measured by the census's absence probe."""

    evidence = NoAbsenceProbes(
        tuple(
            f"src/agent_comms/{path}"
            for path in (
                "typed_table.py",
                "backend.py",
                "input_drain.py",
                "turn_watchdog.py",
                "commander/order.py",
                "attempt_start.py",
                "todos.py",
            )
        ),
    )


class TypeSwitchesPhase(RemainingSlopPhase):
    """2.4: the AST's own visitor replaces switches on its node types."""

    evidence = PatternAbsent(r"isinstance\([^)]*ast\.", ("src/agent_comms/commander/order.py",))


class OrderSendPhase(RemainingSlopPhase):
    """2.5: ``send`` keeps only the dispatch; its parts move to the order types."""

    evidence = NoGodFunction("src/agent_comms/commander/order.py", "send")


class ExternalBoundariesPhase(RemainingSlopPhase):
    """Wave 3."""

    evidence = PatternAbsent(r"^\s*except Exception\b")


class ThreadNameOwnersPhase(RemainingSlopPhase):
    """4.1: the registry owns a thread's name; the coordinator keys by lookup."""

    evidence = PatternAbsent(r"\bowner_thread\b")


class TurnOwnerIdentityPhase(RemainingSlopPhase):
    """4.5: a turn's owner is a ProcessIdentity, never a bare pid."""

    evidence = PatternAbsent(r"\bowner_pid\b")


class FormatIdentityPhase(RemainingSlopPhase):
    """4.7: the FieldCodec-derived schema is a stored format's identity."""

    evidence = PatternAbsent(
        r"format_fingerprint|_canonical_schema|convert_format|source_fingerprints"
    )


class DirectoryFsyncPhase(RemainingSlopPhase):
    """5.1: one atomic file owner; no caller passes an fsync flag."""

    evidence = PatternAbsent(r"\bfsync_parent\b")


class PlatformFactsPhase(RemainingSlopPhase):
    """5.2: Platform owns private-mode checks."""

    evidence = PatternAbsent(
        r"\bos\.name\b",
        (
            "src/agent_comms/goal_history.py",
            "src/agent_comms/goal_attempts.py",
            "src/agent_comms/coordination_database.py",
            "src/agent_comms/maintenance_barrier.py",
        ),
    )


class HandPrecedencePhase(RemainingSlopPhase):
    """6.1: the family's declaration order owns precedence, never an integer."""

    evidence = PatternAbsent(
        r"^\s*(precedence|priority|classification_priority|proof_order|action_order)"
        r"(: ClassVar\[int\])? = [0-9]"
    )


class FailureReasonPhase(RemainingSlopPhase):
    """6.2: TurnFailure members are the reason."""

    after = (HandPrecedencePhase,)
    evidence = PatternAbsent(r"\bFailureReason\b")


class DeadToolsPhase(RemainingSlopPhase):
    """8: unreferenced tools."""

    evidence = PathsAbsent(
        ("tools/codex_duplicate_execution_hook.py", "tools/prove_commander_dispatch.py")
    )


class MaintenanceGatePhase(RemainingSlopPhase):
    """9.1: a carried file's kind refuses writers during maintenance; no hand-placed check."""

    evidence = PatternAbsent(
        r"assert_open_unlocked\(\)",
        ("src/agent_comms/agent_activity.py", "src/agent_comms/wire_log.py"),
    )


class TranscriptHoldPhase(RemainingSlopPhase):
    """9.3: the gate replaced the installer's transcript lock hold."""

    evidence = PatternAbsent(r"transcripts_held|HOLD_TRANSCRIPTS", ("src/agent_comms/installer",))


class InstallerFileIdentityPhase(RemainingSlopPhase):
    """9.2: the installer identifies a file by its declaring RootFile type."""

    after = (TranscriptHoldPhase,)
    evidence = PatternAbsent(
        r"rewritten: tuple\[str|endswith\(\(\"\.lock\"|name\[:-4\]",
        ("src/agent_comms/installer",),
    )


class ContextRequestKeyPhase(RemainingSlopPhase):
    """4.2: context requests are keyed by the incarnation lookup, never the current name."""

    evidence = PatternAbsent(r"\bthread_name\b", ("src/agent_comms/context_store.py",))


class TurnStatePhase(RemainingSlopPhase):
    """4.4: a FinishedTurn member of the turn-record family owns the last finished turn."""

    evidence = PatternAbsent(r"\blast_finished_turn_id\b")


class WireRenamesPhase(RemainingSlopPhase):
    """4.6: our own wire keys equal the field names; no old claim-scope spellings remain."""

    evidence = PatternAbsent(r"WireName\(\"admission")


class OwnedDispatchPhase(RemainingSlopPhase):
    """5.4: MroDispatch replaces the inbox double dispatch; one compact JSON encoder."""

    evidence = AllEvidence(
        (
            PatternAbsent(r"class TurnInboxReceiver\b"),
            PatternAbsent(
                r"""separators\s*=\s*\(\s*["'],["']\s*,\s*["']:["']\s*\)""",
                ("src", ":!src/agent_comms/field_codec.py"),
            ),
            FunctionDeclared("compact", ("src/agent_comms/field_codec.py",)),
        )
    )


class KindFamiliesPhase(RemainingSlopPhase):
    """6.3: kinds are families whose members carry their behaviour and spelling."""

    evidence = AllEvidence(
        (
            PatternAbsent(
                r"class (TagAction|ExecutionOrigin|WhenBusy|ControlClassification"
                r"|MembershipChange)\b"
            ),
            # SQLite's ViewKind(CatalogKind) is already a behavior-owning member.
            PatternAbsent(r"class ViewKind\((StrEnum|Enum)\)"),
        )
    )


class RostersPhase(RemainingSlopPhase):
    """6.4: remove the known internal registries; external producer ABI stays independent."""

    evidence = AllEvidence(
        (
            PatternAbsent(r"^\s*THREAD_CAPABILITIES\s*[:=]"),
            ClassFlagsAbsent(r"^\s*serves_(new_)?threads\s*[:=]"),
            *(
                ClassFlagsAbsent(r"^\s*" + attribute + r"\s*[:=]", owners=(owner,))
                for owner, attribute in (
                    (QualifiedName.of("agent_comms.installer.probes.carry.Carry"), "declared"),
                    (QualifiedName.of("agent_comms.private_registry_guard.RegistryPrivacy"), "kinds"),
                    (QualifiedName.of("agent_comms.thread_identity.BirthlessIncarnation"), "members"),
                    (QualifiedName.of("agent_comms.tool_calls.CliToolArguments"), "types"),
                    (QualifiedName.of("agent_comms.pi_events.PiEvent"), "_start_phases"),
                    (QualifiedName.of("agent_comms.pi_events.PiEvent"), "_end_events"),
                )
            ),
        )
    )


class RecordsPhase(RemainingSlopPhase):
    """7: dead connectivity facets go; document updates return their result."""

    evidence = AllEvidence(
        (
            PatternAbsent(r"[C]onnectivityFacet|[o]bserve_connectivity"),
            PatternAbsent(
                r"^\s*nonlocal\b",
                (
                    "src/agent_comms/read_ledger.py",
                    "src/agent_comms/relationships.py",
                    "src/agent_comms/shared_ledger.py",
                    "src/agent_comms/input_disposition.py",
                    "src/agent_comms/owner_lifecycle.py",
                ),
            ),
        )
    )


class FleetAwarenessPhase(CommanderNativePhase, declared_name=None):
    """Phase 10: fleet awareness (after the four plans)."""

    after = (SkillPhase, BusDeletionPhase, InheritanceCleanupPhase, TransitionScaffoldingPhase)


class OneHolderPerWorktreePhase(FleetAwarenessPhase):
    """One holder per worktree, refused at order time."""

    evidence = ClassDeclared("SharedWorktreeRefusal", ("src/agent_comms",))


class DriftOverlapRulesPhase(FleetAwarenessPhase):
    """Drift and overlap as standing rules."""

    evidence = AllEvidence(
        (
            ClassDeclared("BranchBehindMain", ("src/agent_comms",)),
            ClassDeclared("SharedFileBranches", ("src/agent_comms",)),
        )
    )


class SpinningUnitPhase(FleetAwarenessPhase):
    """Spinning as a UnitState member."""

    evidence = ClassDeclared("SpinningUnit", ("src/agent_comms",))


class UnpushedWorkPhase(FleetAwarenessPhase):
    """Unpushed work as a standing rule."""

    evidence = ClassDeclared("UnpushedBranch", ("src/agent_comms",))


class EmptyPullRequestPhase(FleetAwarenessPhase):
    """Empty PRs as a standing rule."""

    evidence = ClassDeclared("EmptyPullRequest", ("src/agent_comms",))


class DeliveryReviewPhase(FleetAwarenessPhase):
    """Review is triggered by delivery."""

    evidence = ClassDeclared("DeliveredHeadReview", ("src/agent_comms",))


class LostToolsPhase(FleetAwarenessPhase):
    """Lost tools are a bus fact."""

    evidence = ClassDeclared("LostTools", ("src/agent_comms",))


class FleetReplayPhase(FleetAwarenessPhase):
    """Replaying the copied fleet state raises each incident from Core alone, once each, with no script."""

    after = (
        OneHolderPerWorktreePhase,
        DriftOverlapRulesPhase,
        SpinningUnitPhase,
        UnpushedWorkPhase,
        EmptyPullRequestPhase,
        DeliveryReviewPhase,
        LostToolsPhase,
    )
    evidence = FunctionDeclared("test_replay_raises_each_incident_once", ("tests",))


class NominalIdTypePhase(Id128NominalPhase):
    """1: an id is a nominal value type; FieldCodec decodes and mints it, the wire unchanged."""

    evidence = AllEvidence(
        (
            ClassDeclared("NominalText", ("src/agent_comms/field_codec.py",)),
            ClassDeclared("NominalSpelling", ("src/agent_comms/field_codec.py",)),
            ClassDeclared("Id128", ("src/agent_comms/field_codec.py",)),
            ClassDeclared("MessageId", ("src/agent_comms/field_codec.py",)),
            *(ClassDeclared(kind.__name__, ("src/agent_comms/field_codec.py",))
              for kind in Id128.__subclasses__()),
            PatternAbsent(
                r"def mint\(cls(, text: str)?\) -> str:", ("src/agent_comms/field_codec.py",)
            ),
        )
    )


class PlainStrIdRatchetPhase(Id128NominalPhase):
    """2: no file gains a plain ``str`` annotation on an id-named field or parameter."""

    after = (NominalIdTypePhase,)
    evidence = PatternPresent(r"^class PlainStrIdAnnotation\(", ("tools/debt_ratchet.py",))


class ClaimIdsPhase(Id128NominalPhase):
    """3: claim nonces, operation ids and generations are ``ClaimId``; their checks go."""

    after = (NominalIdTypePhase,)
    evidence = AllEvidence(
        (
            PatternAbsent(
                r"\b(nonce|operation_id|generation): "
                r"(str\b|Id128\b|Annotated\[(str|Id128)\b)",
                Id128NominalPhase.paths,
            ),
            PatternAbsent(
                r"Id128Text\.decode\(self\.(nonce|operation_id)\)",
                Id128NominalPhase.paths,
            ),
        )
    )


class MessageIdsPhase(Id128NominalPhase):
    """4: bus message ids are ``MessageId``."""

    after = (NominalIdTypePhase,)
    evidence = AllEvidence(
        (
            PatternAbsent(
                r"\b(message_id|source_message_id): "
                r"(str\b|Id128\b|Annotated\[(str|Id128)\b)",
                Id128NominalPhase.paths,
            ),
            PatternAbsent(
                r"Annotated\[str, *MessageIdText\]",
                Id128NominalPhase.paths,
            ),
        )
    )


class TurnIdsPhase(Id128NominalPhase):
    """5: Core turn ids are ``TurnId``; their re-decodes go."""

    after = (NominalIdTypePhase,)
    evidence = AllEvidence(
        (
            PatternAbsent(
                r"\b(turn_id|finished_turn_id|report_turn_id|current_turn_id)"
                r": (str\b|Id128\b|Annotated\[(str|Id128)\b)",
                Id128NominalPhase.paths,
            ),
            PatternAbsent(
                r"Id128Text\.(decode|from_text)\((self\.)?turn_id\)",
                Id128NominalPhase.paths,
            ),
        )
    )


class GoalIdsPhase(Id128NominalPhase):
    """6: goal ids are ``GoalId``, minted by it, never by a hand-written ``uuid4().hex``."""

    after = (NominalIdTypePhase,)
    evidence = PatternAbsent(
        r"\b(goal_id|original_goal_id): (str\b|Id128\b|Annotated\[(str|Id128)\b)",
        Id128NominalPhase.paths,
    )


class RootIdsPhase(Id128NominalPhase):
    """7: bus roots and recipient lookups are ``RootId``; their re-decodes go."""

    after = (NominalIdTypePhase,)
    evidence = AllEvidence(
        (
            PatternAbsent(
                r"\b(wire_root_id|root_id|lookup|recipient_lookup): "
                r"(str\b|Id128\b|Annotated\[(str|Id128)\b)",
                Id128NominalPhase.paths,
            ),
            PatternAbsent(
                r"Id128Text\.decode\((self\.)?(owner\.)?(wire_root_id|root_id|lookup)\)",
                Id128NominalPhase.paths,
            ),
            PatternAbsent(
                r"FieldCodec\.decode\(Annotated\[str, *Id128Text\]",
                Id128NominalPhase.paths,
            ),
        )
    )


class InputIdsPhase(Id128NominalPhase):
    """8: input ids are ``InputId``; their re-decodes and constructor checks go."""

    after = (NominalIdTypePhase,)
    evidence = AllEvidence(
        (
            PatternAbsent(
                r"\b(input_id|accepted_input_id|native_id): "
                r"(str\b|Id128\b|Annotated\[(str|Id128)\b)",
                Id128NominalPhase.paths,
            ),
            PatternAbsent(
                r"Id128Text\.(decode|from_text)\((self\.)?(input_id|native_id)\)",
                Id128NominalPhase.paths,
            ),
        )
    )


class RedecodesDeletedPhase(Id128NominalPhase):
    """9: no id is decoded twice or minted by hand, and a plain ``str`` field refuses an id."""

    after = (PlainStrIdRatchetPhase, InputIdsPhase)
    evidence = AllEvidence(
        (
            PatternAbsent(r"Id128Text\.(decode|from_text)\("),
            PatternAbsent(r"Annotated\[str, *(Id128Text|MessageIdText)\]"),
            PatternAbsent(r"uuid4\(\)\.hex([^\[]|$)", ("src", ":!src/agent_comms/field_codec.py")),
            PatternAbsent(r"isinstance\(\w+, NominalText\)", ("src/agent_comms/field_codec.py",)),
        )
    )
