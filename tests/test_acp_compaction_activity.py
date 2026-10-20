"""Native compaction phases reach the activity used by every sidebar."""

import os

import pytest
from agent_comms.acp_failure import NativeTurnError

import agent_comms.agent_events as ae
from delivery_owner_fixture import canonical_agent
from agent_comms.activity import ActivityState
from agent_comms.child_process import ProcessIdentity
from agent_comms.comms import wire
from agent_comms.pi_vocabulary import ThresholdCompactionReason
from agent_comms.turn_phase import ModelWaitPhase
from test_acp import fake_input_started


