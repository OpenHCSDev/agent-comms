"""Selected execution and ACP followups share one live input lifetime."""

from bus_history import read_history
from agent_comms.messages import NoticeRole
import asyncio
from contextlib import asynccontextmanager, nullcontext
from dataclasses import replace

import pytest
from acp import RequestError

import agent_comms.agent_events as events
import agent_comms.coordinated_runtime as coordinated_runtime
import agent_comms.private_nk_entrypoint as private_nk_entrypoint
from agent_comms.acp_extension import InputDeliveryChangedUpdate, decode_updates
from agent_comms.input_attempt import ClearedUnsentInput, ReservedInput, StartedInput
from agent_comms.native_arguments import NativeArguments
from agent_comms.send_outcome import AuthorityChangedSend, GrantedSend
from agent_comms.tracked_turn import TrackedTurnSession
from test_acp_private_nk_delivery import _session
from test_acp_private_nk_delivery import tmp_path as private_root_fixture
from test_coordinated_runtime import _fake_model
from stored_inputs import stored_inputs
from selected_owner_rpc_fixture import scripted_owner, recorded_request, requests

tmp_path = private_root_fixture


