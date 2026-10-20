"""Original owner inputs use the goal authority captured at local claim_scope.

These tests control backend timing, exercising the real claim_scope callbacks,
private goal ledger, and durable UNKNOWN/STARTED transition without a provider.
"""

from agent_comms.queued_input import InitialInput
import json

import pytest
from agent_comms.acp_failure import NativeTurnError

import agent_comms.agent_events as ae
from agent_comms.schedule_rules import WakeScheduleCheck
from agent_comms.queued_input import InputHandoffRefused
from agent_comms.comms import wire
from agent_comms.goal_actions import SetGoalAction
from agent_comms.goal_generation import ReadyGeneration
from agent_comms.input_disposition import InputDispositions
from agent_comms.send_outcome import AuthorityChangedSend, GrantedSend
from delivery_owner_fixture import canonical_agent
from stored_inputs import stored_inputs


def disposition_rows(agent):
    return list(stored_inputs(agent.inputs.dispositions.root).rows.values())


