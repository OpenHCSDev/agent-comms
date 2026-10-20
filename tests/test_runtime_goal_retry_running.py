"""Owner Retry is durable immediately and launches only after the current turn."""

import asyncio
import os
from contextlib import aclosing

import pytest
from acp import RequestError

import agent_comms.agent_events as ae
import agent_comms.backend as backend
from agent_comms.acp_failure import RequestFailure
from agent_comms.acp_extension import GoalChangedUpdate, decode_updates
from agent_comms.field_codec import FieldCodec
from agent_comms.goal_actions import (
    BlockedGoalAction,
    CompletedGoalAction,
    GoalPrecondition,
    ModelInvocable,
    SetGoalAction,
)
from agent_comms.goal_attempts import GoalAttemptStore
from agent_comms.goal_generation import CompletedGeneration, ReadyGeneration
from agent_comms.goals import Goal
from agent_comms.runtime import RuntimeProxy, socket_path
from stored_inputs import stored_inputs

pytestmark = pytest.mark.skipif(os.name == "nt", reason="POSIX owner socket")


