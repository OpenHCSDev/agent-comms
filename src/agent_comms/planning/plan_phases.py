from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar

from agent_comms.cli_bridges.tool_calls import CliToolArguments
from agent_comms.execution.pi_events import PiEvent
from agent_comms.installer.probes.carry import Carry
from agent_comms.nominal.declared_family import DeclaredFamily
from agent_comms.nominal.field_codec import Id128, MessageId, NominalSpelling, NominalText
from agent_comms.planning.order_phase import OrderPhase
from agent_comms.planning.orders import Todo
from agent_comms.planning.phase_artifacts import RecordedArtifact
from agent_comms.planning.phase_evidence import (
    AllEvidence,
    AuditPatternPresent,
    BusStorageAbsent,
    ClassDeclared,
    ClassFlagsAbsent,
    ClassRostersAbsent,
    CommitMerged,
    DocumentPatternPresent,
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
    QualifiedName,
    RestatedMembersAbsent,
)
from agent_comms.planning.phase_tasks import PhaseTask, UnspecifiedPhaseTask
from agent_comms.planning.plan_reading import PlanReading
from agent_comms.registry.private_registry_guard import RegistryPrivacy
from agent_comms.registry.thread_identity import BirthlessIncarnation
from agent_comms.storage.store_files import SqliteStore
from agent_comms_hooks.process_claim import ProcessSingleton
from agent_comms_hooks.queue_bypass import QueueBypassGuard


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

    document = "plans/MODULE-OWNERSHIP-PLAN.md"
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
        return tuple(todo for todo in cls.serving(reading.todos) if todo.state.open and todo.holder)

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


class TeamNativePhase(PlanPhase, declared_name=None):
    plan = TeamNativePlan


class VirtualBusPhase(PlanPhase, declared_name=None):
    plan = VirtualBusPlan


class MultipleInheritancePhase(PlanPhase, declared_name=None):
    plan = MultipleInheritancePlan


class RemainingSlopPhase(PlanPhase, declared_name=None):
    plan = RemainingSlopPlan


class Id128NominalPhase(PlanPhase, declared_name=None):
    plan = Id128NominalPlan
    paths: ClassVar[tuple[str, ...]] = ("src", ":!src/agent_comms/skills/")

    @classmethod
    def absent(cls, pattern: str) -> PatternAbsent:
        return PatternAbsent(pattern, cls.paths)

    @classmethod
    def plain(cls, names: str) -> PatternAbsent:
        """No field or parameter named ``names`` keeps a plain ``str`` or bare ``Id128``."""
        return cls.absent(rf"\b({names}): (str\b|Id128\b|Annotated\[(str|Id128)\b)")


class StoreGuardsPhase(PlanPhase, declared_name=None):
    plan = StoreGuardsPlan
    owners: ClassVar[tuple[str, ...]] = (
        ":!src/agent_comms/storage/store_files.py", ":!src/agent_comms/storage/locked_store.py",
        ":!src/agent_comms/skills/",
    )


class OwnerAsksPhase(PlanPhase, declared_name=None):
    plan = OwnerAsksPlan


class ToadAskPhase(OwnerAsksPhase, declared_name=None):
    """An ask whose code lands in Toad. Evidence reads only the plan repository's main, so the
    plan document records the merged Toad commit under the phase's own name."""

    @classmethod
    def phase_evidence(cls) -> PhaseEvidence:
        return DocumentPatternPresent(
            rf"^- {cls.declared_name} landed: OpenHCSDev/toad@[0-9a-f]{{40}}$",
            (OwnerAsksPlan.document,),
        )


class RetainedStorePhase(TeamNativePhase):
    evidence = CommitMerged("a75deaf04")


class OneOrderOwnerPhase(TeamNativePhase):
    evidence = CommitMerged("9293095d9")


class DesignReconciledPhase(TeamNativePhase):
    after = (OneOrderOwnerPhase,)
    evidence = CommitMerged("64fbd8bc1")


class RecordPlanePhase(TeamNativePhase):
    after = (RetainedStorePhase,)
    evidence = CommitMerged("465a27988")


class ReviewQueuePhase(TeamNativePhase):
    """A verdict per PR head is recorded, and QueueBypass declares each bypass's pin and
    recorded-verdict obligations. No GitHub ruleset, merge queue or bypass ban is required:
    one owner works alone and merges with --admin (Tristan, 2026-11-06)."""

    after = (RecordPlanePhase,)
    evidence = AllEvidence((
        ClassDeclared("HeadReviewRecord", ("src/agent_comms/planning/orders.py",)),
        ClassDeclared(QueueBypassGuard.__name__, ("src/agent_comms_hooks/queue_bypass.py",)),
    ))


TEAM_PACKAGE = "src/agent_comms/team/"


class ControlPlanePhase(TeamNativePhase):
    """Phase 7's deletes: Core owns every signal these scripts raised."""

    after = (RecordPlanePhase,)
    evidence = PathsAbsent(
        tuple(
            TEAM_PACKAGE + name
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


class StoryPhase(TeamNativePhase):
    """A view of the native records, not a prerequisite for their cutover: both story tools
    exist and the separate roast step is deleted."""

    after = (RecordPlanePhase,)
    evidence = AllEvidence(
        (
            ClassDeclared("CommsOrderStoryTool", ("src/agent_comms/transport/tools/orders.py",)),
            ClassDeclared("CommsTeamStoryTool", ("src/agent_comms/transport/tools/orders.py",)),
            PatternAbsent(r"^def roast\b", (TEAM_PACKAGE + "judge.py",)),
        )
    )


class TeamPagePhase(TeamNativePhase, declared_name="team_page"):
    """A view of the native records and control plane, independent of cutover."""

    after = (RecordPlanePhase, ControlPlanePhase)
    evidence = AllEvidence(
        (
            ClassDeclared("TeamHud", ("src",)),
            PatternPresent(r"\bcomms_hud\b", ("src",)),
        )
    )


class ParityPhase(TeamNativePhase):
    """The native capability owners, not a second matrix of demonstrations, decide completion."""

    after = (RecordPlanePhase, ReviewQueuePhase, ControlPlanePhase, TeamPagePhase)
    evidence = ParityEvidence(tuple(phase.phase_evidence() for phase in after))


class CutoverPhase(TeamNativePhase):
    """The native evidence passes and the machine-wide teammate configuration is gone."""

    after = (ParityPhase,)
    evidence = AllEvidence((ParityPhase.phase_evidence(), PatternAbsent(r"\bTeamConfig\b")))


class SkillPhase(TeamNativePhase):
    """The skill names no script."""

    after = (CutoverPhase,)
    evidence = PatternAbsent(r"agent-comms-team", ("src/agent_comms/skills/comms-team",))


OLD_LOG_READ = r"\bWireLog\b|wire_log"


class BusInterfacePhase(VirtualBusPhase):
    evidence = CommitMerged("4792954e")


class BusWriterPhase(VirtualBusPhase):
    """W: the one writer onto ``Bus.append``."""

    after = (BusInterfacePhase,)
    evidence = PatternAbsent(
        "[a]ppend_private_unlocked",
        ("src/agent_comms/messaging/bus_publication.py", "src/agent_comms/messaging/publisher.py"),
    )


class BusDeliveryReadersPhase(VirtualBusPhase):
    """B1: delivery, wake, cursor and notification readers on ``seq``."""

    after = (BusWriterPhase,)
    evidence = PatternAbsent(
        OLD_LOG_READ,
        (
            "src/agent_comms/waking/wake_candidate_index.py",
            "src/agent_comms/notification_assignment.py",
            "src/agent_comms/messaging/read_ledger.py",
            "src/agent_comms/execution/cursor_publication.py",
            "src/agent_comms/planning/goal_waits.py",
        ),
    )


class BusHistoryReadersPhase(VirtualBusPhase):
    """B2: history, presentation and transcript readers on ``seq``."""

    after = (BusWriterPhase,)
    evidence = PatternAbsent(
        OLD_LOG_READ,
        (
            "src/agent_comms/messaging/historical_views.py",
            "src/agent_comms/presentation.py",
            "src/agent_comms/transcript/transcripts.py",
            "src/agent_comms/execution/transcript_sources.py",
            "src/agent_comms/installer/active_route.py",
            "src/agent_comms/execution/acp.py",
            "src/agent_comms/comms.py",
            "src/agent_comms/messaging/message_bus.py",
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
    """1C. ``GoalDisplay.visible`` stays by the teammate's decision, so ``visible`` is not read."""

    evidence = PatternAbsent(rf"^\s+({SMALL_FAMILY_FLAGS})" + FLAG)


class TriggersPhase(MultipleInheritancePhase):
    """1D: every trigger derives from its table's capability in ``typed_table.py``."""

    evidence = PatternAbsent(
        r"BEFORE (UPDATE|DELETE|INSERT)", ("src", ":!src/agent_comms/storage/typed_table.py")
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
                        "coordination/tables/attempts.py",
                        "execution/native_input_record.py",
                        "participant_store.py",
                    ),
                ),
                (
                    ("trigger_bodies",),
                    ("coordination/response.py", "execution/native_prompt_binding.py", "private_sidecar.py"),
                ),
                (
                    ("__init__",),
                    ("durable_turn.py", "execution_store.py", "exporting.py", "publisher.py"),
                ),
                (("wire_member",), ("pi_payloads.py", "nominal/field_codec.py")),
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
                        "coordination/response.py",
                        "storage/typed_table.py",
                    ),
                ),
                (
                    ("references",),
                    (
                        "coordination/tables/attempts.py",
                        "coordination/tables/recovery.py",
                        "coordination/tables/responses.py",
                    ),
                ),
                (
                    ("annotation_disclosure", "disclosure_for"),
                    ("pi_payloads.py", "execution/turn_context.py", "annotation_disclosure.py"),
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


class NominalIdTypePhase(Id128NominalPhase):
    """1: an id is a nominal value type; FieldCodec decodes and mints it, the wire unchanged."""

    evidence = AllEvidence(
        (
            *(ClassDeclared(kind.__name__, ("src/agent_comms/nominal/field_codec.py",))
              for kind in (
                  NominalText, NominalSpelling, Id128, MessageId, *Id128.__subclasses__(),
              )),
            PatternAbsent(
                r"def mint\(cls(, text: str)?\) -> str:", ("src/agent_comms/nominal/field_codec.py",)
            ),
        )
    )


class PlainStrIdRatchetPhase(Id128NominalPhase):
    """2: no file gains a plain ``str`` annotation on an id-named field or parameter."""

    after = (NominalIdTypePhase,)
    evidence = AuditPatternPresent(r"^class PlainStrIdAnnotation\(")


class ClaimIdsPhase(Id128NominalPhase):
    """3: claim nonces, operation ids and generations are ``ClaimId``; their checks go."""

    after = (NominalIdTypePhase,)
    evidence = AllEvidence(
        (
            Id128NominalPhase.plain("nonce|operation_id|generation"),
            Id128NominalPhase.absent(r"Id128Text\.decode\(self\.(nonce|operation_id)\)"),
        )
    )


class MessageIdsPhase(Id128NominalPhase):
    """4: bus message ids are ``MessageId``."""

    after = (NominalIdTypePhase,)
    evidence = AllEvidence(
        (
            Id128NominalPhase.plain("message_id|source_message_id"),
            Id128NominalPhase.absent(r"Annotated\[str, *MessageIdText\]"),
        )
    )


class TurnIdsPhase(Id128NominalPhase):
    """5: Core turn ids are ``TurnId``; their re-decodes go."""

    after = (NominalIdTypePhase,)
    evidence = AllEvidence(
        (
            Id128NominalPhase.plain("turn_id|finished_turn_id|report_turn_id|current_turn_id"),
            Id128NominalPhase.absent(r"Id128Text\.(decode|from_text)\((self\.)?turn_id\)"),
        )
    )


class GoalIdsPhase(Id128NominalPhase):
    """6: goal ids are ``GoalId``, minted by it, never minted by hand."""

    after = (NominalIdTypePhase,)
    evidence = Id128NominalPhase.plain("goal_id|original_goal_id")


class RootIdsPhase(Id128NominalPhase):
    """7: bus roots and recipient lookups are ``RootId``; their re-decodes go."""

    after = (NominalIdTypePhase,)
    evidence = AllEvidence(
        (
            Id128NominalPhase.plain("wire_root_id|root_id|lookup|recipient_lookup"),
            Id128NominalPhase.absent(
                r"Id128Text\.decode\((self\.)?(owner\.)?(wire_root_id|root_id|lookup)\)"),
            Id128NominalPhase.absent(r"FieldCodec\.decode\(Annotated\[str, *Id128Text\]"),
        )
    )


class InputIdsPhase(Id128NominalPhase):
    """8: input ids are ``InputId``; their re-decodes and constructor checks go."""

    after = (NominalIdTypePhase,)
    evidence = AllEvidence(
        (
            Id128NominalPhase.plain("input_id|native_id"),
            Id128NominalPhase.absent(
                r"Id128Text\.(decode|from_text)\((self\.)?(input_id|native_id)\)"),
        )
    )


class RedecodesDeletedPhase(Id128NominalPhase):
    """9: no id is decoded twice or minted by hand, and a plain ``str`` field refuses an id."""

    after = (PlainStrIdRatchetPhase, InputIdsPhase)
    evidence = AllEvidence(
        (
            PatternAbsent(r"Id128Text\.(decode|from_text)\("),
            PatternAbsent(r"Annotated\[str, *(Id128Text|MessageIdText)\]"),
            PatternAbsent(r"uuid4\(\)\.hex([^\[]|$)", ("src", ":!src/agent_comms/nominal/field_codec.py")),
            PatternAbsent(r"isinstance\(\w+, NominalText\)", ("src/agent_comms/nominal/field_codec.py",)),
        )
    )


class OwnTypeChecksPhase(MultipleInheritancePhase):
    """Batch 3."""

    after = (FieldDirectivesPhase, RedecodesDeletedPhase)
    evidence = OwnClassChecksAbsent()


class InheritanceCleanupPhase(MultipleInheritancePhase):
    """Batch 4, the deletion closure: no capability shell left with one member, no member
    restating its base's answer, no probe for a batch 1 flag, and no literal roster of
    classes."""

    after = (OwnTypeChecksPhase,)
    evidence = AllEvidence(
        (
            ParentsShared(paths=("src/agent_comms/nominal/lifecycle_capability.py",)),
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
                "storage/typed_table.py",
                "backend.py",
                "execution/input_drain.py",
                "execution/turn_watchdog.py",
                "commander/order.py",
                "attempt_start.py",
                "todos.py",
            )
        ),
    )


class TypeSwitchesPhase(RemainingSlopPhase):
    """2.4: the AST's own visitor replaces switches on its node types."""

    evidence = PatternAbsent(r"isinstance\([^)]*ast\.", ("src/agent_comms/team/order.py",))


class OrderSendPhase(RemainingSlopPhase):
    """2.5: ``send`` keeps only the dispatch; its parts move to the order types."""

    evidence = NoGodFunction("src/agent_comms/team/order.py", "send")


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

    # Bracketed so the declaration is not itself a match: unbracketed, this line was the one
    # occurrence left in src and the phase could never be done.
    evidence = PatternAbsent(
        r"[f]ormat_fingerprint|_[c]anonical_schema|[c]onvert_format|[s]ource_fingerprints"
    )


class DirectoryFsyncPhase(RemainingSlopPhase):
    """5.1: one atomic file owner; no caller passes an fsync flag."""

    evidence = PatternAbsent(r"\bfsync_parent\b")


class PlatformFactsPhase(RemainingSlopPhase):
    """5.2: Platform owns private-mode checks."""

    evidence = PatternAbsent(
        r"\bos\.name\b",
        (
            "src/agent_comms/planning/goal_history.py",
            "src/agent_comms/planning/goal_attempts.py",
            "src/agent_comms/coordination/database.py",
            "src/agent_comms/storage/maintenance_barrier.py",
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
        ("src/agent_comms/registry/agent_activity.py", "src/agent_comms/wire_log.py"),
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
                ("src", ":!src/agent_comms/nominal/field_codec.py"),
            ),
            FunctionDeclared("compact", ("src/agent_comms/nominal/field_codec.py",)),
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
                    (QualifiedName.of_class(Carry), "declared"),
                    (QualifiedName.of_class(RegistryPrivacy), "kinds"),
                    (QualifiedName.of_class(BirthlessIncarnation), "members"),
                    (QualifiedName.of_class(CliToolArguments), "types"),
                    (QualifiedName.of_class(PiEvent), "_start_phases"),
                    (QualifiedName.of_class(PiEvent), "_end_events"),
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
                    "src/agent_comms/messaging/read_ledger.py",
                    "src/agent_comms/registry/relationships.py",
                    "src/agent_comms/registry/shared_ledger.py",
                    "src/agent_comms/input_disposition.py",
                    "src/agent_comms/execution/owner_lifecycle.py",
                ),
            ),
        )
    )


class TeamAwarenessPhase(TeamNativePhase, declared_name=None):
    """Phase 10: fleet awareness (after the four plans)."""

    after = (SkillPhase, BusDeletionPhase, InheritanceCleanupPhase, TransitionScaffoldingPhase)


class OneHolderPerWorktreePhase(TeamAwarenessPhase):
    """One holder per worktree, refused at order time."""

    evidence = ClassDeclared("SharedWorktreeRefusal", ("src/agent_comms",))


class DriftOverlapRulesPhase(TeamAwarenessPhase):
    """Drift and overlap as standing rules."""

    evidence = AllEvidence(
        (
            ClassDeclared("BranchBehindMain", ("src/agent_comms",)),
            ClassDeclared("ConflictingBranches", ("src/agent_comms",)),
        )
    )


class SpinningTeammatePhase(TeamAwarenessPhase, declared_name="spinning_unit"):
    """Spinning as a TeammateState member."""

    evidence = ClassDeclared("SpinningTeammate", ("src/agent_comms",))


class UnpushedWorkPhase(TeamAwarenessPhase):
    """Unpushed work as a standing rule."""

    evidence = ClassDeclared("UnpushedBranch", ("src/agent_comms",))


class EmptyPullRequestPhase(TeamAwarenessPhase):
    """Empty PRs as a standing rule."""

    evidence = ClassDeclared("EmptyPullRequest", ("src/agent_comms",))


class DeliveryReviewPhase(TeamAwarenessPhase):
    """Review is triggered by delivery."""

    evidence = ClassDeclared("DeliveredHeadReview", ("src/agent_comms",))


class LostToolsPhase(TeamAwarenessPhase):
    """Lost tools are a bus fact."""

    evidence = ClassDeclared("LostTools", ("src/agent_comms",))


class TeamReplayPhase(TeamAwarenessPhase, declared_name="fleet_replay"):
    """Replaying the copied fleet state raises each incident from Core alone, once each, with no script."""

    after = (
        OneHolderPerWorktreePhase,
        DriftOverlapRulesPhase,
        SpinningTeammatePhase,
        UnpushedWorkPhase,
        EmptyPullRequestPhase,
        DeliveryReviewPhase,
        LostToolsPhase,
    )
    evidence = FunctionDeclared("test_replay_raises_each_incident_once", ("tests",))


class StoreGuardFamiliesPhase(StoreGuardsPhase):
    """1: each store's guard is declared on its family; SQLite and hand flocks go."""

    evidence = AllEvidence(
        (
            ClassDeclared(SqliteStore.__name__, ("src/agent_comms/storage/store_files.py",)),
            ClassDeclared(ProcessSingleton.__name__, ("src/agent_comms_hooks/process_claim.py",)),
            PatternAbsent(r"fcntl\.flock\(",
                          ("src", ":!src/agent_comms_hooks/process_claim.py", ":!src/agent_comms/skills/")),
            PatternAbsent(r"_store_lock\(", ("src/agent_comms/storage/typed_table.py",)),
            PatternAbsent(r"_store_lock\(path\), closing", ("src/agent_comms/storage/locked_store.py",)),
        )
    )


class ReadWriteMembersPhase(StoreGuardsPhase):
    """2: stores expose reading and writing; no caller picks a mode, a wait or ``locked()``."""

    after = (StoreGuardFamiliesPhase,)
    evidence = AllEvidence(
        (
            ClassDeclared("SingleFileStore", ("src/agent_comms/storage/store_files.py",)),
            ClassDeclared("MultiFileStore", ("src/agent_comms/storage/store_files.py",)),
            PatternAbsent(r"(\.locked|_store_lock|_async_store_lock|ordered)\([^)]*(mode|contention)=",
                          ("src", ":!src/agent_comms/skills/")),
            PatternAbsent(r"def locked\(", ("src", *StoreGuardsPhase.owners)),
        )
    )


class BoundedWaitPhase(StoreGuardsPhase):
    """3: every guard's wait is bounded and declared on its family."""

    after = (ReadWriteMembersPhase,)
    evidence = PatternAbsent(r"UNBOUNDED[_]CONTENTION|Unbounded[C]ontention",
                             ("src", ":!src/agent_comms/skills/"))


class MaintenanceInWritePhase(StoreGuardsPhase):
    """4: a guard's write asks its store kind about maintenance; no hand-placed check."""

    after = (ReadWriteMembersPhase,)
    evidence = PatternAbsent(
        r"assert_open_unlocked\(\)|require_maintenance_open\((path)?\)",
        ("src", *StoreGuardsPhase.owners, ":!src/agent_comms/storage/typed_table.py",
         ":!src/agent_comms/storage/maintenance_barrier.py"),
    )


class LockCensusPhase(StoreGuardsPhase):
    """5: the NRA gate counts lock calls outside the store owners and refuses growth."""

    evidence = AuditPatternPresent(r"^class StoreLockCalls\(")


class ScopedReadsPhase(StoreGuardsPhase):
    """6: a store lock is held for its copy or its change only; no caller names a lock."""

    after = (ReadWriteMembersPhase,)
    evidence = PatternAbsent(
        r"guarded_(read|write)(_async)?\(|StoreLock\.ordered|reading_snapshot\(|read_snapshot_unlocked\("
        r"|[^f]\._read_unlocked\(|store\.reading\(|CapturedSelection\.reading\(|catalog\.reading\("
        r"|reads\.reading\(",
        ("src", *StoreGuardsPhase.owners, ":!src/agent_comms/registry/registry_store.py"),
    )


class KeymapK2K7Phase(ToadAskPhase):
    """Toad keymap K2 to K7: remappable F1/F2, focus-modal bindings, a binding menu (toad#1212)."""


class PageQuarterScrollPhase(ToadAskPhase):
    """Plain PageUp/PageDown scroll a quarter page; toad#1233 did only the shifted keys."""


class LoadingSpinnerPhase(ToadAskPhase):
    """The top loading spinner is back; both were removed instead of keeping it."""


class TeamLatencyPhase(OwnerAsksPhase, declared_name="team_latency"):
    """Each teammate verb answers in under 5 s; they take about 30 s."""

    evidence = FunctionDeclared("test_team_verb_answers_under_five_seconds", ("tests",))


class DeliveryLatencyPhase(OwnerAsksPhase):
    """A bus message reaches its recipient on its next tool call; delivery slowed at 01:57 UTC
    on 2026-10-25."""

    evidence = FunctionDeclared("test_message_injects_into_next_tool_call", ("tests",))


class RamBudgetPhase(OwnerAsksPhase):
    """The fleet's memory owner is found and capped; desktop free RAM fell to 2.2 GB."""

    evidence = FunctionDeclared("test_team_memory_stays_under_its_cap", ("tests",))


class UnflattenModulesPhase(OwnerAsksPhase):
    """The package structure for the flat modules of ``src/agent_comms`` is a declared plan."""

    evidence = ClassDeclared(PackageStructurePlan.__name__, ("src/agent_comms/planning/plan_phases.py",))


class PackageStructurePhase(PlanPhase, declared_name=None):
    plan = PackageStructurePlan


class PackageDeclarationsPhase(PackageStructurePhase):
    after = (RamBudgetPhase, ScopedReadsPhase)
    evidence = PathsAbsent((
        "src/agent_comms/field_codec.py", "src/agent_comms/declared_family.py",
        "src/agent_comms/json_schema.py", "src/agent_comms/sealed.py",
    ))


class PackageStoragePhase(PackageStructurePhase):
    after = (PackageDeclarationsPhase,)
    evidence = PathsAbsent((
        "src/agent_comms/store_files.py", "src/agent_comms/locked_store.py",
        "src/agent_comms/private_path.py", "src/agent_comms/maintenance_barrier.py",
        "src/agent_comms/typed_table.py",
    ))


class PackageCoordinationPhase(PackageStructurePhase):
    after = (PackageStoragePhase,)
    evidence = PathsAbsent((
        "src/agent_comms/coordinator.py", "src/agent_comms/coordinator_vocabulary.py",
        "src/agent_comms/coordination_tables", "src/agent_comms/coordination_cohort.py",
        "src/agent_comms/coordination_contracts.py", "src/agent_comms/coordination_database.py",
        "src/agent_comms/coordination_errors.py", "src/agent_comms/coordination_response.py",
        "src/agent_comms/coordination_results.py", "src/agent_comms/coordination_schema.py",
        "src/agent_comms/coordination_session.py", "src/agent_comms/coordination_snapshot.py",
        "src/agent_comms/coordination_work.py",
    ))


class PackageExecutionPhase(PackageStructurePhase):
    after = (PackageCoordinationPhase,)
    evidence = PathsAbsent((
        "src/agent_comms/input_drain.py", "src/agent_comms/session_lifecycle.py",
        "src/agent_comms/owner_lifecycle.py", "src/agent_comms/owned_turn.py",
    ))


class PackageConsumerClosurePhase(PackageStructurePhase):
    after = (PackageExecutionPhase,)
    evidence = PathsAbsent((
        "src/agent_comms/registration.py", "src/agent_comms/registry_store.py",
        "src/agent_comms/registry_document.py", "src/agent_comms/threads.py",
        "src/agent_comms/goal_waits.py",
    ))


class BoardLineLockTextPhase(OwnerAsksPhase):
    """A board line says what the heartbeat gate does, which since #3345 is print, not refuse;
    a JUDGE line names its verbs without their argparse usage."""

    evidence = AllEvidence(
        (
            PatternAbsent(r"LOCKS your writing verbs", ("src",)),
            PatternAbsent(r"verb\.usage\(\)", ("src/agent_comms/team/standing.py",)),
        )
    )


class ClosedUnmergedPrsPhase(OwnerAsksPhase):
    """A standing rule flags a PR closed unmerged that holds commits main lacks (lost: toad
    #1080, #1095)."""

    evidence = ClassDeclared("ClosedUnmergedPullRequest", ("src/agent_comms",))


class ModuleOwnershipPhase(PlanPhase, declared_name=None):
    """A phase of the module-ownership plan; every one of them owns a destination, not a move."""

    plan = ModuleOwnershipPlan


class ModuleMapPhase(ModuleOwnershipPhase):
    """P0: retained source census, full/raw scan and the first batch's ownership trace.

    A completion sentence proves none of these. Each artifact must be present on main
    at the contents recorded after its evidence was checked.
    """

    evidence = AllEvidence((
        RecordedArtifact(ModuleOwnershipPlan.evidence_manifest,
                         "docs/plan-evidence/module-map-counts-51d17a5cb.json"),
        RecordedArtifact(ModuleOwnershipPlan.evidence_manifest,
                         "docs/plan-evidence/nra-p0-full-51d17a5cb.json"),
        RecordedArtifact(ModuleOwnershipPlan.evidence_manifest,
                         "docs/plan-evidence/module-map-imports-51d17a5cb.json"),
        RecordedArtifact(ModuleOwnershipPlan.evidence_manifest,
                         "docs/plan-evidence/R-star-p1.md"),
    ))


class PlanningOwnersPhase(ModuleOwnershipPhase):
    """P1: `todos.py`, `tools.py` and `standing_rules.py` split at their owners into
    `planning/` and `transport/`, with every consumer following them."""

    after = (ModuleMapPhase,)
    evidence = AllEvidence((
        PathsAbsent(("src/agent_comms/todos.py", "src/agent_comms/tools.py",
                     "src/agent_comms/standing_rules.py")),
        PatternAbsent(r"from agent_comms[.](todos|tools|standing_rules)\b", ("src",)),
        PatternAbsent(r"import agent_comms[.](todos|tools|standing_rules)\b", ("src",)),
    ))


class ProjectOwnersPhase(ModuleOwnershipPhase):
    """P2: `projects/` owns the repository, checkout and storage owners."""

    after = (PlanningOwnersPhase,)
    evidence = AllEvidence((
        PathsAbsent(("src/agent_comms/repo.py", "src/agent_comms/storage_rules.py",
                     "src/agent_comms/storage_projection.py")),
        PatternAbsent(r"from agent_comms[.](repo|storage_rules|storage_projection)\b", ("src",)),
    ))


class MessagingOwnersPhase(ModuleOwnershipPhase):
    """P3: `messaging/` owns the bus, channels and conversation owners."""

    after = (ProjectOwnersPhase,)
    evidence = PathsAbsent(("src/agent_comms/message_bus.py", "src/agent_comms/messaging.py"))


class ExecutionOwnersPhase(ModuleOwnershipPhase):
    """P4: `execution/` takes the runtime mass: workers, turns and native sessions."""

    after = (MessagingOwnersPhase,)
    evidence = AllEvidence((
        PathsAbsent(("src/agent_comms/worker.py", "src/agent_comms/execution/native_pi.py")),
        PatternAbsent(r"from agent_comms[.](worker|native_pi)\b", ("src",)),
    ))


class ModuleSlopPhase(ModuleOwnershipPhase):
    """P5: the slop the moves expose, by census delta in the PR body, top patterns first."""

    after = (ExecutionOwnersPhase,)
    evidence = AllEvidence((
        DocumentPatternPresent(r"^- P5 census: ", (ModuleOwnershipPlan.document,)),
        PatternAbsent(r"\bkind == ", ("src/agent_comms",)),
    ))


class PhaseEvidenceClosurePhase(ModuleOwnershipPhase):
    """P6: no phase may be satisfied by a file merely disappearing.

    A predicate that only asks whether a path still exists proves nothing about ownership,
    parity or consumers, which is how a plan can read Done while 69% of the code never moved.
    """

    after = (ModuleSlopPhase,)
    evidence = PatternAbsent(r"^    evidence = PathsAbsent\(", ("src/agent_comms/planning/plan_phases.py",))
