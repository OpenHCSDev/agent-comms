"""Fake-RPC provider-free handshake; no pinned Pi CLI or external model."""

from __future__ import annotations

from agent_comms.native_package import VerifiedNativePackage, pinned_native_package
import sys
from pathlib import Path

from native_proof_cases import child_writer_imports, read_proof_rows, write_proof_rows

import pytest

import agent_comms.native_pi as native
import agent_comms.selected_tool_broker as broker
from agent_comms.child_process import AttachedChild
from agent_comms.selected_tool_broker import (
    SelectedToolMode,
    consume_selected_slot,
    record_selected_terminal,
)
from agent_comms.tracked_turn import TrackedTurnSession
from agent_comms.coordinated_runtime import IndependentExecution
from agent_comms.native_startup import NativeStartup
from agent_comms.turn_watchdog import ModelWait
from agent_comms.pi_native_backend import PersistentPiSession
from agent_comms.selected_session import SelectedSession

INPUT_ID = "a" * 32


