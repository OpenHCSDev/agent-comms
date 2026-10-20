from dataclasses import replace

import pytest

from agent_comms.field_codec import FieldCodec
from agent_comms.standing_rules import PlanIdle
from agent_comms.plan_phases import (
    CommanderPagePhase,
    ControlPlanePhase,
    CutoverPhase,
    PlanPhase,
    RecordPlanePhase,
    SkillPhase,
    StoryPhase,
    UnplannedPhase,
)
from agent_comms.todos import (
    DoneTodoState,
    FoldedProblemOrder,
    InstalledProblemOrder,
    ProblemOrder,
    OperationDelivery,
    UnorderedProblemOrder,
)
from agent_comms.tools import CommsOrderServeTool
from agent_comms.work_dispatch import CheckedPhaseTask

from test_commander_problem_orders import create


def accepted(todo):
    installed = todo.order.copy_as(
        InstalledProblemOrder, delivery=OperationDelivery(),
        works="The declared whole phase worked through its acceptance run.",
        artifact="phase-acceptance.txt",
    )
    return replace(todo, state=DoneTodoState, assignment=None, order=installed)


def task(todo):
    return CheckedPhaseTask(
        problem=todo.id, repo=todo.repo, text=todo.text,
        order=todo.order, deadline_seconds=300,
    )


