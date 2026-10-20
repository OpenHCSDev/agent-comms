"""Actual pinned CLI through ordinary TurnSession; localhost provider only."""

import asyncio
import json
from functools import partial
from pathlib import Path

from native_proof_cases import read_proof_rows, write_proof_rows

import pytest

import agent_comms.agent_events as events
import agent_comms.backend as backend
from agent_comms.acp_extension import InputDeliveryChangedUpdate, QueuePromptRequest, decode_updates, encode_request
from agent_comms.cohort_schema import install_private_cohort_schema
from agent_comms.comms import Comms
from agent_comms.coordinator import Coordination
from agent_comms.input_attempt import ClearedNativeInput
from agent_comms.input_disposition import InputDispositions
from agent_comms.threads import Thread
from stored_inputs import stored_inputs


from native_backend_fixture import wait_for


