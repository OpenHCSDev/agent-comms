"""Fresh private foreground owner protocol; model boundary is strictly offline fake.

These tests do not prove a provider spend ceiling, copied-Pi trust or release.
"""

from __future__ import annotations

from bus_history import read_history
from agent_comms.native_package import VerifiedNativePackage, pinned_native_package
from conftest import publish_initial
import asyncio
import multiprocessing
import os
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

import agent_comms.nk_foreground as foreground
from agent_comms.child_process import ProcessIdentity
from agent_comms.cohort_schema import install_private_cohort_schema
from agent_comms.comms import Comms
from agent_comms.coordinated_runtime_schema import install_native_runtime_schema
from agent_comms.coordination_cohort import accept_delivery_cohort
from agent_comms.coordination_errors import PublicationActivationBlocked
from agent_comms.coordination_response import install_private_response_schema
from agent_comms.coordinator import Coordination
from agent_comms.errors import RelationViolationError
from agent_comms.native_runtime_input import NativeRuntimeInput
from agent_comms.nk_foreground import reserve_foreground_owner
from agent_comms.threads import Thread
from agent_comms.tracked_turn import TrackedTurnSession
from test_coordinated_runtime import _fake_model

pytestmark = pytest.mark.skipif(
    sys.platform != "linux", reason="private foreground N/K requires Linux /var/tmp and fork"
)


@pytest.fixture
def sender(private_comms) -> Path:
    """The publishing participant and the shared worktree under the private root."""
    work = private_comms.root / "work"
    work.mkdir(mode=0o700)
    private_comms.registry.declare(
        private_comms.registry.new_thread('sender', frozenset(), str(work), process_identity=ProcessIdentity.capture(os.getpid()))
    )
    return work


