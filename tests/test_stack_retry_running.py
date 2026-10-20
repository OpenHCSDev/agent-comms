"""Real saved native owner: Retry defers until the original response retires."""

import asyncio
import json
import os

import pytest

from agent_comms.field_codec import FieldCodec
from agent_comms.goal_actions import BlockedGoalAction, GoalPrecondition, PausedGoalAction, SetGoalAction
from agent_comms.goal_attempts import GoalAttemptStore
from agent_comms.goal_generation import ReadyGeneration
from agent_comms.goals import Goal
from agent_comms.runtime import RuntimeProxy, socket_path
from stored_inputs import stored_inputs


