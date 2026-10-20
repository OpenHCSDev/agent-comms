"""Installed selected coding journey through real CLI/tools; localhost responses only."""
from agent_comms.pi_vocabulary import OffThinkingLevel

import asyncio
import json
import os
import time
from dataclasses import replace

import pytest

from agent_comms.child_process import AttachedChild
from agent_comms.channels import ANY_CHANNEL
from agent_comms.coordinated_runtime import SelectedExecution
from agent_comms.coordination_errors import CoordinationError
from agent_comms.coordinator import Coordination
from agent_comms.native_pi import NativePiUnavailable
from agent_comms.native_runtime_input import NativeRuntimeInput
from compaction_loopback import LoopbackProvider
from native_proof_cases import read_proof_rows
from delivery_owner_fixture import canonical_agent
from test_coordinated_runtime import _root


class CodingProvider(LoopbackProvider):
    def __init__(self, seconds):
        super().__init__(status=200, text="Completed real coding tool.")
        self.seconds = seconds
        self.round = 0

    def response_chunks(self):
        self.round += 1
        if self.round == 1:
            yield {
                "tool_calls": [
                    {
                        "index": 0,
                        "id": "actual_sleep",
                        "type": "function",
                        "function": {
                            "name": "bash",
                            "arguments": json.dumps(
                                {
                                    "command": (
                                        "printf native-started > native-started; "
                                        f"sleep {self.seconds}; "
                                        "printf native-tool-finished"
                                    ),
                                    "timeout": self.seconds + 10,
                                }
                            ),
                        },
                    }
                ]
            }, None
            yield {}, "tool_calls"
        else:
            yield from super().response_chunks()


