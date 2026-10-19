"""Each native/current workflow declares its identity once."""

from ..plan_phases import (
    CommanderPagePhase,
    ControlPlanePhase,
    CutoverPhase,
    OneOrderOwnerPhase,
    RecordPlanePhase,
    ReviewQueuePhase,
    StoryPhase,
)
from . import ParityRow


class CreateProblemParityRow(ParityRow):
    number = 1
    capability = "Create a problem with owner, order and deadline"
    current = "`order.py problem`; `problems.json`"
    native = "`CommsTodoCreateTool`"
    phases = (OneOrderOwnerPhase,)


class AssignOrderParityRow(ParityRow):
    number = 2
    capability = "Assign, reassign, drop an order"
    current = "`order.py send`, `--reassign`, `drop`; `deadlines.json`"
    native = "`CommsOrderTool`, `CommsTodoTool` assignment and release"
    phases = (OneOrderOwnerPhase,)


class DeliveredCommitParityRow(ParityRow):
    number = 3
    capability = "Complete only on the delivered commit"
    current = "`order.py done`"
    native = "`CommsOrderDoneTool`: merged to main, another branch, or not merged"
    phases = (OneOrderOwnerPhase, RecordPlanePhase)


class InstalledEvidenceParityRow(ParityRow):
    number = 4
    capability = "Close with installed evidence"
    current = "`order.py close`; `closed.jsonl`"
    native = "`CommsProblemCloseTool`"
    phases = (OneOrderOwnerPhase,)


class BoardParityRow(ParityRow):
    number = 5
    capability = "Board, overdue, silent, spinning"
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
    native = "`Strike` rows, author-declared `StrikeLimit`, one `comms_fire`"
    phases = (RecordPlanePhase,)


class VerdictParityRow(ParityRow):
    number = 8
    capability = "Verdicts on a unit's tool calls"
    current = "`judge.py ok`, `back`; `verdicts.jsonl`"
    native = "Order `Verdict`; send-back revises the Goal and records its strike atomically"
    phases = (RecordPlanePhase,)


class PlainWordsParityRow(ParityRow):
    number = 9
    capability = "Plain words in verdicts and strikes"
    current = "judge and order checks"
    native = "`PlainText`"
    phases = (RecordPlanePhase,)


class OrderWordingParityRow(ParityRow):
    number = 10
    capability = "Refuse soft or vague orders"
    current = "`order.py`"
    native = "`OrderText` on every order action"
    phases = (RecordPlanePhase,)


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
    current = "`rules.py`; `rules.jsonl`; maximum age"
    native = "Rule instruction segments in the commander's turn context, with provenance"
    phases = (ControlPlanePhase,)


class LoadForkingParityRow(ParityRow):
    number = 17
    capability = "Load and forking"
    current = "`capacity.py`: six units, ten-minute beat age; `forks/`"
    native = "Reporting-only `commander_load.CommanderLoad.of`; P253 removes forking policy"
    phases = (ControlPlanePhase,)


class GrowthAlarmParityRow(ParityRow):
    number = 18
    capability = "Growth alarm"
    current = "`growth_watch.py`: merges growing src by more than 40 net lines"
    native = "`commander.config.Repo.main_changes` → `commander_measurements.MergeGrowth.read`"
    phases = (ControlPlanePhase,)


class StorageHygieneParityRow(ParityRow):
    number = 19
    capability = "Storage hygiene"
    current = "`storage.py`: checkout limit, cleanup under 30 GB, allocation floor 5 GB"
    native = "`UnitStorage`, derived from worktree paths and free disk"
    phases = (ControlPlanePhase,)


class OrderOnlyContactParityRow(ParityRow):
    number = 20
    capability = "Commander reaches units only through orders"
    current = "`send_guard.py` (PreToolUse hook)"
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
    current = "scratch `rts/order.py send` (`Works when:`)"
    native = "`OrderText` requires the run; `CommsProblemCloseTool.works` checks it"
    phases = (RecordPlanePhase,)


class TracedTransferParityRow(ParityRow):
    number = 24
    capability = "Reassigning past two owners needs the commander's trace"
    current = "scratch `order.py send --reassign --traced`"
    native = "Transfer reads Todo owner history and requires traced source location"
    phases = (RecordPlanePhase,)


class UnitWordingParityRow(ParityRow):
    number = 25
    capability = "Unit wording gets a correction, not a strike"
    current = "scratch `vibe_watch.py`"
    native = "`PlainText` for commander verdicts/strikes; unit wording gets correction"
    phases = (RecordPlanePhase,)


class DecisionAlertsParityRow(ParityRow):
    number = 26
    capability = "Alerts carry only decisions"
    current = "none; every line interrupts"
    native = "`messages.AlertKind.require_publishable`, `tools.CommsDecisionTool`"
    phases = (ControlPlanePhase,)


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
    native = "Reporting-only `commander_load.CommanderLoad.of`: orders, overdue, edits and live forks"
    phases = (ControlPlanePhase,)


class FinishedWorkTransferParityRow(ParityRow):
    number = 29
    capability = "No handoff of finished, checked work"
    current = "none; scratch transfer moved P507 after its fix was pushed and checked"
    native = "Transfer refuses delivered checked work; that owner merges; check is reused"
    phases = (RecordPlanePhase,)


class BuildBoundProofParityRow(ParityRow):
    number = 30
    capability = "A proof is bound to the build it ran on"
    current = "none; screenshots and logs are bare paths"
    native = "Build run records runtime and Core/Toad revisions; close requires delivered commit"
    phases = (RecordPlanePhase,)


class DeletionTargetParityRow(ParityRow):
    number = 31
    capability = "An order states its intent as a deletion target"
    current = "none; P520 renamed a decision rather than deleting it"
    native = "Order owns deletion family/site/count; delivery refuses nonzero or renamed decisions"
    phases = (RecordPlanePhase,)


class UnitTimeParityRow(ParityRow):
    number = 32
    capability = "Where units' time goes, classified"
    current = "none; activity.log lacks durations and turn boundaries"
    native = (
        "`UnitTime`: model, build/check/test/shell, dependency/commander waits and idle; "
        "classify large waits"
    )
    phases = (CommanderPagePhase, ControlPlanePhase)


class OrderProgressParityRow(ParityRow):
    number = 33
    capability = "Order progress beside its deadline; the unit never sees a due time"
    current = "`order.py board`, `overdue`, `due`, `send`; `rotate.py`"
    native = (
        "Derived `OrderProgress`; merged work draws no strike; first commit 10 min, "
        "later progress 15 min STALL; units see no due time"
    )
    phases = (OneOrderOwnerPhase, ControlPlanePhase)


class MultipleCommandersParityRow(ParityRow):
    number = 34
    capability = "Several commanders on one machine and bus, one per project"
    current = "none; a second commander.json install overwrites the first"
    native = (
        "Registry commander/project ownership; caller from --from; scoped orders, strikes, "
        "load, board, alarms and forks; raw-send guard; duplicate project commander refused"
    )
    phases = (OneOrderOwnerPhase, CutoverPhase)
