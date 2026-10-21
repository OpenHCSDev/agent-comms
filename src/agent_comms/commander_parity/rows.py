"""Each native/current workflow declares its identity once."""

from agent_comms.commander_parity import ParityComparison, ParityRow, UnverifiedParity
from agent_comms.commander_parity.delivery import DeliveredCommitCase
from agent_comms.commander_parity.handoff import FinishedHandoffCase
from agent_comms.commander_parity.lifecycle import (
    AssignOrderCase,
    BuildBoundProofCase,
    CreateProblemCase,
    InstalledEvidenceCase,
    StrikeLimitCase,
    verify_order_progress,
)
from agent_comms.commander_parity.load import LoadComparison
from agent_comms.commander_parity.measurements import StorageComparison, measured_load
from agent_comms.commander_parity.story import StoryCase
from agent_comms.commander_parity.verdicts import VerdictComparison
from agent_comms.commander_parity.wording import (
    ApplicationRunComparison,
    DecisionAlertsComparison,
    OrderWordingComparison,
    PlainWordsComparison,
    UnitWordingComparison,
)
from agent_comms.plan_phases import (
    CommanderPagePhase,
    ControlPlanePhase,
    CutoverPhase,
    OneOrderOwnerPhase,
    ReviewQueuePhase,
    StoryPhase,
)


class CreateProblemParityRow(ParityRow):
    number = 1
    capability = "Create a problem with owner, order and review timer"
    current = "`order.py problem`; `problems.json`"
    native = "`CommsTodoCreateTool`"
    phases = (OneOrderOwnerPhase,)

    comparison = CreateProblemCase.read


class AssignOrderParityRow(ParityRow):
    number = 2
    capability = "Assign, reassign, drop an order"
    current = "`order.py send`, `--reassign`, `drop`; `deadlines.json`"
    native = "`CommsOrderTool`, `CommsTodoTool` assignment and release"
    phases = (OneOrderOwnerPhase,)

    comparison = AssignOrderCase.read


class DeliveredCommitParityRow(ParityRow):
    number = 3
    capability = "Complete only on the delivered commit"
    current = "`order.py done`"
    native = "`CommsOrderDoneTool`: merged to main, another branch, or not merged"
    phases = (OneOrderOwnerPhase,)
    comparison = DeliveredCommitCase.read


class InstalledEvidenceParityRow(ParityRow):
    number = 4
    capability = "Close with installed evidence"
    current = "`order.py close`; `closed.jsonl`"
    native = "`CommsProblemCloseTool`"
    phases = (OneOrderOwnerPhase,)

    comparison = InstalledEvidenceCase.read


class BoardParityRow(ParityRow):
    number = 5
    capability = "Board, review timers up, silent, spinning"
    current = "`order.py board`, `overdue`; `fleet-board.md`"
    native = "`CommsTodoListTool` and the fleet tool, derived"
    phases = (OneOrderOwnerPhase,)


class BranchHygieneParityRow(ParityRow):
    number = 6
    capability = "Branch hygiene"
    current = "`branches-*.txt` by hand"
    native = "`CommsWorkBranchesTool`"
    phases = (OneOrderOwnerPhase,)


class StrikeLimitParityRow(ParityRow):
    number = 7
    capability = "Strikes and the strike limit"
    current = "`order.py strike`; `strikes.jsonl`"
    native = "`StrikeTodo`, `ObjectiveStrike`, `ProblemGoalOrder.StrikeLimit`, `TodoStore.fire`"
    phases = ()

    comparison = StrikeLimitCase.read


class VerdictParityRow(ParityRow):
    number = 8
    capability = "Verdicts on a unit's tool calls"
    current = "`judge.py ok`, `back`; both invoke `CommsVerdictTool`"
    native = "`CommsVerdictTool`, `SendBackVerdict`: one retained event and Goal revision"
    phases = ()
    comparison = VerdictComparison.read


class PlainWordsParityRow(ParityRow):
    number = 9
    capability = "Plain words in verdicts and strikes"
    current = "judge and order checks"
    native = "`PlainText`, `StrikeReason`"
    phases = ()

    @classmethod
    def verify(cls) -> ParityComparison:
        return PlainWordsComparison.read()


class OrderWordingParityRow(ParityRow):
    number = 10
    capability = "Refuse soft or vague orders"
    current = "`order.py`"
    native = "`OrderText`, `CheckedProblemOrder.require_wording`"
    phases = ()

    @classmethod
    def verify(cls) -> ParityComparison:
        return OrderWordingComparison.read()


class OrderWorkParityRow(ParityRow):
    number = 11
    capability = "What a unit did during its order"
    current = "`tool_watch.py`; `edits.jsonl`; `judge.py show`"
    native = "`comms_order_work`: both transcript families by `tool_call_id`"
    phases = (ReviewQueuePhase,)


class UnjudgedParityRow(ParityRow):
    number = 12
    capability = "What the commander has not judged yet"
    current = "`judge.py unjudged`; `vibe_watch.py`"
    native = "`comms_unjudged`: calls and unit messages after the last verdict"
    phases = (ReviewQueuePhase,)


class PublicStoryParityRow(ParityRow):
    number = 13
    capability = "Public story and roasts"
    current = "`fleet-narrative.md`; `judge.py roast`"
    native = "`comms_order_story`, `comms_fleet_story`; send-back reason posted once"
    phases = (StoryPhase,)
    comparison = StoryCase.read


class ScheduledWakeParityRow(ParityRow):
    number = 14
    capability = "Heartbeat: next unit every minute"
    current = "`rotate.py` (60 s loop)"
    native = "Core-owned scheduled commander wake"
    phases = (ControlPlanePhase,)


class StandingRulesParityRow(ParityRow):
    number = 15
    capability = "Standing rules broken right now"
    current = "`rotate.py broken_rules`"
    native = "`StandingRule` members derive violations on read"
    phases = (ControlPlanePhase,)


class RuleReviewParityRow(ParityRow):
    number = 16
    capability = "Rule review"
    current = "`rules.py review` prints every rule; the session hook rereads them in full"
    native = "Rule instruction segments in the commander's turn context, with provenance"
    phases = (ControlPlanePhase,)


class LoadForkingParityRow(ParityRow):
    number = 17
    capability = "Load and forking"
    current = "`agent-comms-commander capacity`: load and live fork reports; ten-minute beat age"
    native = (
        "Reporting-only `commander.capacity.Load.of`; no capacity refusal or automatic fork policy"
    )
    phases = (ControlPlanePhase,)

    @classmethod
    def verify(cls) -> ParityComparison:
        return LoadComparison.read()


class StorageHygieneParityRow(ParityRow):
    number = 19
    capability = "Storage hygiene"
    current = "`StandingRule.broken` storage delivery (formerly `storage.py`)"
    native = "`StorageViolation.violations`, `DiskLow.violations`; live `StorageRule` thresholds"
    phases = (ControlPlanePhase,)

    @classmethod
    def verify(cls) -> ParityComparison:
        return StorageComparison.read()


class OrderOnlyContactParityRow(ParityRow):
    number = 20
    capability = "Commander reaches units only through orders"
    current = "Nothing: the PreToolUse send guard was deleted"
    native = "Harness hook checking Core order tools"
    phases = (ControlPlanePhase,)


class ConfigurationParityRow(ParityRow):
    number = 21
    capability = "Paths, repositories, commander identity"
    current = "`CommanderConfig` from `commander.json`"
    native = "Core project and registry configuration"
    phases = (CutoverPhase,)


class CommanderPageParityRow(ParityRow):
    number = 22
    capability = "Seeing the fleet: board, strikes, queue, story, alarms"
    current = "`fleet-board.md`, `order.py board`, `rotate.py`, `judge.py`"
    native = "Toad commander page rendering Core through `ui_model`"
    phases = (CommanderPagePhase,)


class ApplicationRunParityRow(ParityRow):
    number = 23
    capability = "An order states the user-visible run that closes it"
    current = "`Commander.close` through the copied-bus command runner"
    native = "`ApplicationOrderRun.require_closing`; `CommsProblemCloseTool.operation_for`"
    phases = ()

    @classmethod
    def verify(cls) -> ParityComparison:
        return ApplicationRunComparison.read()


class TracedTransferParityRow(ParityRow):
    number = 24
    capability = "Reassigning past two owners needs the commander's trace"
    current = "scratch `order.py send --reassign --traced`"
    native = "`Assignment.require_trace`, `Todo.assigned`"
    phases = ()


class UnitWordingParityRow(ParityRow):
    number = 25
    capability = "Unit wording gets a correction, not a strike"
    current = "`Commander.call(CommsCorrectWordingTool)`"
    native = "`CommsCorrectWordingTool.apply`: correction message, unchanged strikes"
    phases = ()

    @classmethod
    def verify(cls) -> ParityComparison:
        return UnitWordingComparison.read()


class DecisionAlertsParityRow(ParityRow):
    number = 26
    capability = "Authored decisions are validated; alerts stay open"
    current = "`CommsAuthoredTaskTool.apply` publishes authored decisions as info notices"
    native = (
        "`Message.require_decision`; publisher validates author, turn and scope; "
        "bare `AlertKind` publication remains allowed"
    )
    phases = (ControlPlanePhase,)

    @classmethod
    def verify(cls) -> ParityComparison:
        return DecisionAlertsComparison.read()


class MomentumParityRow(ParityRow):
    number = 27
    capability = "Momentum prediction per unit, compared with the outcome"
    current = "none"
    native = (
        "Prediction record and later outcome on commander page; "
        "`CommitMomentum` is a partial candidate"
    )
    phases = (CommanderPagePhase,)


class MeasuredLoadParityRow(ParityRow):
    number = 28
    capability = "Measured load and self-report"
    current = "`capacity.py` unit count"
    native = (
        "`commander.capacity.Load.of` reports orders, review timers up, edits and forks; "
        "self-report comparison unverified"
    )
    phases = (ControlPlanePhase,)

    @classmethod
    def verify(cls) -> ParityComparison:
        return measured_load()


class FinishedWorkTransferParityRow(ParityRow):
    number = 29
    capability = "No handoff of finished, checked work"
    current = "none; scratch transfer moved P507 after its fix was pushed and checked"
    native = "`DeliveredProblemOrder.require_transfer`: the checked delivery stays with its owner"
    phases = ()

    comparison = FinishedHandoffCase.read


class BuildBoundProofParityRow(ParityRow):
    number = 30
    capability = "A proof is bound to the build it ran on"
    current = "none; screenshots and logs are bare paths"
    native = "`DeliveredProblemOrder.require_installation`, `RecordedBuildIdentity.installation`"
    phases = ()

    comparison = BuildBoundProofCase.read


class UnitTimeParityRow(ParityRow):
    number = 32
    capability = "Where units' time goes, classified"
    current = "none; activity.log lacks durations and turn boundaries"
    native = (
        "`UnitTime`: model, build/check/test/shell, dependency/commander waits and idle; "
        "classify large waits"
    )
    phases = (CommanderPagePhase, ControlPlanePhase)

    @classmethod
    def verify(cls) -> ParityComparison:
        return UnverifiedParity(f"Missing native owner: {cls.native}")


class OrderProgressParityRow(ParityRow):
    number = 33
    capability = "Order progress beside its review timer; the unit never sees it"
    current = "`order.py board`, `overdue`, `due`, `send`; `rotate.py`"
    native = (
        "Derived `OrderProgress`; uncommitted edits count as work; no clock strikes or "
        "releases; the commander's review timer is hidden from units"
    )
    phases = (OneOrderOwnerPhase, ControlPlanePhase)

    comparison = staticmethod(verify_order_progress)


class MultipleCommandersParityRow(ParityRow):
    number = 34
    capability = "Several commanders on one machine and bus, one per project"
    current = "none; a second commander.json install overwrites the first"
    native = (
        "Registry commander/project ownership; caller from --from; scoped orders, strikes, "
        "load, board, alarms and forks; raw-send guard; duplicate project commander refused"
    )
    phases = (OneOrderOwnerPhase, CutoverPhase)
