"""The rest of the matrix, explicitly unverified until real comparisons exist.

Each declaration owns its identity and native integration phases. Owning a row
is not completing that phase; view phases never become phase prerequisites.
"""

from ..parity_checks import UnimplementedParityCheck
from ..plan_phases import (
    CommanderPagePhase, ControlPlanePhase, CutoverPhase, OneOrderOwnerPhase,
    RecordPlanePhase, ReviewQueuePhase, StoryPhase,
)


class CreateProblemParityCheck(UnimplementedParityCheck):
    number = 1
    capability = "Create a problem with owner, order and deadline"
    phases = (OneOrderOwnerPhase,)


class AssignOrderParityCheck(UnimplementedParityCheck):
    number = 2
    capability = "Assign, reassign and drop an order"
    phases = (OneOrderOwnerPhase,)


class DeliveredCommitParityCheck(UnimplementedParityCheck):
    number = 3
    capability = "Complete only on the delivered commit"
    phases = (OneOrderOwnerPhase, RecordPlanePhase)


class InstalledEvidenceParityCheck(UnimplementedParityCheck):
    number = 4
    capability = "Close with installed evidence"
    phases = (OneOrderOwnerPhase,)


class BoardParityCheck(UnimplementedParityCheck):
    number = 5
    capability = "Board, overdue, silent and spinning"
    phases = (OneOrderOwnerPhase,)


class BranchHygieneParityCheck(UnimplementedParityCheck):
    number = 6
    capability = "Branch hygiene"
    phases = (OneOrderOwnerPhase,)


class StrikeLimitParityCheck(UnimplementedParityCheck):
    number = 7
    capability = "Strikes and the strike limit"
    phases = (RecordPlanePhase,)


class VerdictParityCheck(UnimplementedParityCheck):
    number = 8
    capability = "Verdicts on a unit's tool calls"
    phases = (RecordPlanePhase,)


class OrderWorkParityCheck(UnimplementedParityCheck):
    number = 11
    capability = "What a unit did during its order"
    phases = (ReviewQueuePhase,)


class UnjudgedParityCheck(UnimplementedParityCheck):
    number = 12
    capability = "What the commander has not judged yet"
    phases = (ReviewQueuePhase,)


class PublicStoryParityCheck(UnimplementedParityCheck):
    number = 13
    capability = "Public story and roasts"
    phases = (StoryPhase,)


class ScheduledWakeParityCheck(UnimplementedParityCheck):
    number = 14
    capability = "Heartbeat: next unit every minute"
    phases = (ControlPlanePhase,)


class StandingRulesParityCheck(UnimplementedParityCheck):
    number = 15
    capability = "Standing rules broken right now"
    phases = (ControlPlanePhase,)


class RuleReviewParityCheck(UnimplementedParityCheck):
    number = 16
    capability = "Rule review"
    phases = (ControlPlanePhase,)


class LoadForkingParityCheck(UnimplementedParityCheck):
    number = 17
    capability = "Load and forking"
    phases = (ControlPlanePhase,)


class GrowthAlarmParityCheck(UnimplementedParityCheck):
    number = 18
    capability = "Growth alarm"
    phases = (ControlPlanePhase,)


class StorageHygieneParityCheck(UnimplementedParityCheck):
    number = 19
    capability = "Storage hygiene"
    phases = (ControlPlanePhase,)


class OrderOnlyContactParityCheck(UnimplementedParityCheck):
    number = 20
    capability = "Commander reaches units only through orders"
    phases = (ControlPlanePhase,)


class ConfigurationParityCheck(UnimplementedParityCheck):
    number = 21
    capability = "Paths, repositories and commander identity"
    phases = (CutoverPhase,)


class CommanderPageParityCheck(UnimplementedParityCheck):
    number = 22
    capability = "Seeing the fleet: board, strikes, queue, story and alarms"
    phases = (CommanderPagePhase,)


class ApplicationRunParityCheck(UnimplementedParityCheck):
    number = 23
    capability = "An order states the user-visible run that closes it"
    phases = (RecordPlanePhase,)


class TracedTransferParityCheck(UnimplementedParityCheck):
    number = 24
    capability = "Reassigning past two owners needs the commander's trace"
    phases = (RecordPlanePhase,)


class DecisionAlertsParityCheck(UnimplementedParityCheck):
    number = 26
    capability = "Alerts carry only decisions"
    phases = (ControlPlanePhase,)


class MomentumParityCheck(UnimplementedParityCheck):
    number = 27
    capability = "Momentum prediction per unit compared with the outcome"
    phases = (CommanderPagePhase,)


class MeasuredLoadParityCheck(UnimplementedParityCheck):
    number = 28
    capability = "Measured load and self-report"
    phases = (ControlPlanePhase,)


class FinishedWorkTransferParityCheck(UnimplementedParityCheck):
    number = 29
    capability = "No handoff of finished, checked work"
    phases = (RecordPlanePhase,)


class BuildBoundProofParityCheck(UnimplementedParityCheck):
    number = 30
    capability = "A proof is bound to the build it ran on"
    phases = (RecordPlanePhase,)


class DeletionTargetParityCheck(UnimplementedParityCheck):
    number = 31
    capability = "An order states its intent as a deletion target"
    phases = (RecordPlanePhase,)


class UnitTimeParityCheck(UnimplementedParityCheck):
    number = 32
    capability = "Where units' time goes, classified"
    phases = (CommanderPagePhase, ControlPlanePhase)


class OrderProgressParityCheck(UnimplementedParityCheck):
    number = 33
    capability = "Order progress beside its deadline; the unit sees no due time"
    phases = (OneOrderOwnerPhase, ControlPlanePhase)
