"""A UI goal must acquire owner-private launch authority before it is visible."""

from unittest.mock import AsyncMock
from dataclasses import replace

import os

import pytest

from agent_comms.comms import wire
from agent_comms.field_codec import FieldCodec
from agent_comms.goal_actions import BlockedGoalAction, GoalPrecondition
from agent_comms.goal_attempts import GoalAttemptStore
from agent_comms.goal_generation import ReadyGeneration
from agent_comms.goal_states import BlockedGoal
from agent_comms.goals import Goal
from agent_comms.runtime import RuntimeProxy, socket_path
from delivery_owner_fixture import canonical_agent
from agent_comms.tools import invoke_tool

pytestmark = pytest.mark.skipif(os.name == "nt", reason="ACP runtime uses Unix domain sockets")


@pytest.fixture(autouse=True)
def _models_without_pi_discovery(monkeypatch):
    monkeypatch.setenv("AGENT_COMMS_AGENT_MODELS", "openrouter/fake")


