"""Actual saved SDK/OwnedTurn settlement, with only localhost HTTP controlled."""

import asyncio

import pytest

from agent_comms.coordinator import Coordination
from agent_comms.goal_actions import ActiveGoalAction, GoalPrecondition, OwnerInvocable, SetGoalAction
from agent_comms.goal_generation import ReadyGeneration


