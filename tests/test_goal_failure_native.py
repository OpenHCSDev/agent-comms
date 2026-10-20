"""Saved native history -> ACP goal failure -> passive read, without replay."""

import asyncio
import json
import os
from dataclasses import replace

import pytest
from acp import RequestError
from acp.agent.router import build_agent_router

from agent_comms.comms import Comms
from agent_comms.acp_extension import (
    CompactionChangedUpdate,
    decode_updates,
)
from agent_comms.agent_events import CompactionStart, CompactionEnd
from agent_comms.acp_failure import RequestFailure
from agent_comms.diagnostics import FailureReason
from agent_comms.goal_actions import GoalPrecondition, OwnerInvocable, PausedGoalAction, SetGoalAction
from agent_comms.goal_attempts import GoalAttemptStore, UnresolvedAttemptError
from agent_comms.goal_failure_observation import read_failed_turn_projection
from agent_comms.goal_generation import BlockedGeneration, ReadyGeneration
from agent_comms.runtime import RuntimeProxy, socket_path
from delivery_owner_fixture import canonical_agent
from stored_inputs import sent_text, stored_inputs


