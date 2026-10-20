"""Provider-free runner contract for the selected owner tool boundary."""

from __future__ import annotations

from agent_comms.private_bus_checkpoint import ClaimCommits
from dataclasses import replace
from pathlib import Path

import pytest

import agent_comms.coordinated_runtime as runtime
from agent_comms.thread_identity import RecordedThreadIncarnation
from agent_comms.acp import CommsAgent
from agent_comms.comms import Comms
from agent_comms.coordination_errors import IdentityConflict, PublicationActivationBlocked, StaleFence
from agent_comms.coordinator import Coordination
from agent_comms.envelope_claim_transitions import ExistingFileClaim
from agent_comms.errors import RelationViolationError
from agent_comms.native_pi import NativePiUnavailable
from agent_comms.native_input_record import FullNativeExecution
from agent_comms.native_runtime_input import NativeRuntimeInput
from agent_comms.native_claim_epoch import UnrecordedNativeClaim
from agent_comms.selected_actions import SelectedExistingFileWrite
from agent_comms.selected_tool_broker import (
    SelectedToolIntent,
    SelectedToolRequest,
    SelectedWriteArguments,
)
from agent_comms.tracked_turn import TrackedTurnSession
from test_coordinated_runtime import _fake_model, _root


