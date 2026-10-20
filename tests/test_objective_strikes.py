"""One real TodoStore journey drives every objective failure and its public line."""
import subprocess
import time
from contextlib import nullcontext
from dataclasses import replace

import pytest

from agent_comms.channel_targets import CommandTarget
from agent_comms.child_process import OrderCheckFailedError
from agent_comms.commander.standing import UnownedWheelSpin
from agent_comms.goals import CountCheckResult, OrderCheck
from agent_comms.recorded_time import RecordedEventTime
from agent_comms.todos import AssignTodo, CommitDelivery, ObjectiveStrike
from agent_comms.tools import CommsOrderDoneTool
from test_commander_problem_orders import create, working


