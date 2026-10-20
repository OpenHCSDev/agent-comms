"""Order intent, execution progress and commander-ownership parity declarations."""

from ..plan_phases import (
    CommanderPagePhase,
    ControlPlanePhase,
    CutoverPhase,
    OneOrderOwnerPhase,
    RecordPlanePhase,
)
from . import ParityRow


class DeletionTargetParityRow(ParityRow):
    number = 31
    capability = "An order states its intent as a deletion target"
    current = "`Commander.send/done`: order deletion family/site/count"
    native = "`FamilyDeletion`; `CommsTodoCreateTool` and `CommsOrderDoneTool`"
    phases = (RecordPlanePhase,)


class UnitTimeParityRow(ParityRow):
    number = 32
    capability = "Where units' time goes, classified"
    current = "none; activity.log lacks durations and turn boundaries"
    native = "Missing owner: `UnitTime` turn-bounded activity classification"
    phases = (CommanderPagePhase, ControlPlanePhase)


class OrderProgressParityRow(ParityRow):
    number = 33
    capability = "Order progress beside its deadline; the unit never sees a due time"
    current = "`Commander.due/write_board`; current due() posts clocks to its unit"
    native = "`CommsChangeProblemTool` deadline mutation and `CommsTodoListTool` progress"
    phases = (OneOrderOwnerPhase, ControlPlanePhase)


class MultipleCommandersParityRow(ParityRow):
    number = 34
    capability = "Several commanders on one machine and bus, one per project"
    current = "none; a second commander.json install overwrites the first"
    native = "Missing owner: project-scoped commander registry binding"
    phases = (OneOrderOwnerPhase, CutoverPhase)
