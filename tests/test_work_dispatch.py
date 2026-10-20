"""State-level sanity checks, not the required native startup/first-reply proof."""
from agent_comms.repository_name import RepositoryName

from dataclasses import replace

import pytest

from agent_comms.commander.standing import UnownedWheelSpin
from agent_comms.field_codec import FieldCodec
from agent_comms.goal_actions import CreateProblemGoalAction
from agent_comms.plan_phases import RecordPlanePhase
from agent_comms.recorded_time import RecordedEventTime
from agent_comms.todos import DoneTodoState, OperationDelivery, ProblemOrder, StrikeReason
from agent_comms.phase_tasks import UnspecifiedPhaseTask
from agent_comms.work_dispatch import CheckedPhaseTask, ReusedWorkDispatch, WorkDispatch

from test_commander_problem_orders import create, order
from test_standing_rules import working


