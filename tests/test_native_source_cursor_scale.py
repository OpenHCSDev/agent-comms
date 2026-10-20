"""Bounded, provider-free current-owner cursor pagination; no native ACK inference."""

from __future__ import annotations

from conftest import publish_initial
import os
import tempfile
from pathlib import Path

import pytest

import agent_comms.coordinated_runtime as runtime
from agent_comms.thread_identity import RecordedThreadIncarnation
from agent_comms.native_input_record import TriageNativeExecution, FullNativeExecution
from agent_comms.selected_triage import IgnoreSelectedTriage
from agent_comms.channels import ANY_CHANNEL
from agent_comms.child_process import ProcessIdentity
from agent_comms.coordination_cohort import accept_delivery_cohort
from agent_comms.coordination_errors import IdentityConflict, StaleFence
from agent_comms.coordinator import Coordination
from agent_comms.native_runtime_input import CurrentNativeCursor, NativeRuntimeInput
from agent_comms.native_source_cursor import NativeSourceCursor
from agent_comms.proven_source_coverage import SourceCoverage
from agent_comms.threads import Thread
from agent_comms.tracked_turn import TrackedTurnSession
from test_native_prompt_binding import _fake_model, _root


@pytest.fixture
def tmp_path():
    if os.name != "posix" or not Path("/var/tmp").is_dir() or Path("/var").is_symlink():
        pytest.skip("private sealed runtime needs a disposable /var/tmp")
    with tempfile.TemporaryDirectory(prefix="ac-cursor-scale-", dir="/var/tmp") as name:
        yield Path(name)


