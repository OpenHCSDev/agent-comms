"""Real selected native tools over an isolated retained source; localhost only."""
from bus_history import read_history
from agent_comms.pi_vocabulary import OffThinkingLevel

import asyncio
import functools
import hashlib
import json
import os
import shutil
import sqlite3
import tempfile
import time
from dataclasses import replace
from pathlib import Path

import pytest

from agent_comms.thread_identity import RecordedThreadIncarnation
from agent_comms.child_process import AttachedChild
from agent_comms.coordinated_runtime import SelectedExecution
from agent_comms.coordination_errors import CoordinationError
from agent_comms.native_entries import NativeEvidenceRead
from agent_comms.private_path import FileRevision
from agent_comms.tracked_turn import TrackedTurnSession
from compaction_loopback import LoopbackProvider
from test_coordinated_runtime import _root

SOURCE = os.environ.get("NATIVE_TOOL_LATENCY_SOURCE")
pytestmark = pytest.mark.skipif(not SOURCE, reason="Isolated retained source required")


@pytest.fixture
def tmp_path():
    """Runtime requires /var/tmp; retain original fixture proof under our WT."""
    runtime = Path(tempfile.mkdtemp(prefix="arendt-retained-tool-", dir="/var/tmp"))
    artifacts = Path(os.environ["NATIVE_TOOL_LATENCY_ARTIFACTS"])
    artifacts.mkdir(parents=True, mode=0o700, exist_ok=True)
    try:
        yield runtime
    finally:
        shutil.copytree(runtime, artifacts / runtime.name)
        shutil.rmtree(runtime)


class ReadProvider(LoopbackProvider):
    cancel_final = False

    def response_chunks(self):
        if self.posts == 1:
            if self.cancel_final:
                self.response_gate = asyncio.Event()
            for number in range(3):
                yield {"tool_calls": [{
                    "index": number, "id": f"isolated_read_{number}", "type": "function",
                    "function": {"name": "read", "arguments": json.dumps({"path": f"read-{number}.txt"})},
                }]}, None
            yield {}, "tool_calls"
        else:
            yield from super().response_chunks()


