"""Goal polling and owner actions use a real socket without launching a provider."""

from unittest.mock import AsyncMock

import json
import os
from dataclasses import replace

import pytest

from agent_comms.child_process import ProcessIdentity
from agent_comms.acp_extension import TurnChangedUpdate, encode_updates
from agent_comms.comms import wire
from agent_comms.field_codec import FieldCodec
from agent_comms.goal_actions import (
    BlockedGoalAction,
    EditGoalAction,
    GoalPrecondition,
    OwnerInvocable,
    PausedGoalAction,
    SetGoalAction,
    StandbyGoalAction,
)
from agent_comms.goal_generation import CancelledGeneration
from agent_comms.goals import Goal
from agent_comms.runtime import RuntimeProxy, socket_path
from agent_comms.threads import Thread
from delivery_owner_fixture import canonical_agent

pytestmark = pytest.mark.skipif(os.name == "nt", reason="POSIX owner socket")


