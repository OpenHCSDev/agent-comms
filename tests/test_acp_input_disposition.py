"""Saved SDK owner input ownership over actual native pipes and the owner socket."""

from bus_history import read_history
import asyncio
import json
import os
import sys
from dataclasses import replace
from functools import partial
from pathlib import Path

import pytest
from acp import RequestError
from acp.schema import TextContentBlock

import agent_comms.backend as backend
import agent_comms.pi_commands as commands
import agent_comms.pi_events as pi
from agent_comms.thread_identity import RecordedThreadIncarnation
from agent_comms.acp_failure import RequestFailure
from agent_comms.input_attempt import UnresolvedInput
from agent_comms.acp_extension import InputDeliveryChangedUpdate, QueuePromptRequest, SteerPromptRequest, decode_updates, encode_request
from agent_comms.child_process import BoundedRun, ExitedOutcome
from agent_comms.assignment_states import DeliveredAssignment
from agent_comms.cohort_schema import install_private_cohort_schema
from agent_comms.comms import Comms
from agent_comms.config_options import ModelConfigOption
from agent_comms.field_codec import FieldCodec
from agent_comms.following_inputs import OwnerFollowingInputs
from agent_comms.coordination_cohort import sealed_cohort_assignments
from agent_comms.coordinator import Coordination
from agent_comms.threads import Thread
from agent_comms.input_disposition import InputDispositions
from agent_comms.native_pi import NativePiUnavailable
from agent_comms.notification_progress import AnsweredProgress, SentProgress, ShownToModelProgress
from agent_comms.pi_native_backend import PersistentPiSession
from agent_comms.pi_rpc import PiRpcChannel
from agent_comms.runtime import RuntimeProxy, socket_path
from delivery_owner_fixture import canonical_agent
from stored_inputs import sent_text, stored_inputs
from native_backend_fixture import wait_for


class Updates:
    def __init__(self):
        self.rows = []

    async def session_update(self, *, session_id, update):
        self.rows.append(update if isinstance(update, dict) else
                         update.model_dump(by_alias=True, exclude_none=True))


@pytest.mark.refactor_guard
def test_disposition_fixtures_cannot_reintroduce_retired_delivery_engine():
    import ast

    for name in (
        "test_acp_input_disposition.py",
        "test_acp_channel_disposition.py",
        "delivery_owner_fixture.py",
    ):
        tree = ast.parse(Path(__file__).with_name(name).read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute):
                assert node.attr not in {"delivery_cursors", "inbox_cursors", "legacy_through"}
            if isinstance(node, ast.ImportFrom):
                assert not {alias.name for alias in node.names} & {
                    "AcpDeliveryCursors",
                    "DeliveryCursor",
                    "DeliveryDocument",
                    "wire",
                }


