"""Saved selected-owner history through real terminal/uncertain native outcomes.

Only HTTP provider responses and process termination are controlled. The native
package, claim_scope writer, proof journal, bus and SQLite lifetime are real.
"""
from contextlib import closing
from agent_comms.cursor_publication import CursorDelivery
from agent_comms.native_entries import NativeEvidenceScope

from bus_history import read_history
from conftest import publish_initial
from agent_comms.messages import NoticeRole
import asyncio
import json
import os
from collections import deque
from contextlib import asynccontextmanager
from dataclasses import replace
from pathlib import Path

import pytest
from agent_comms.pi_vocabulary import OffThinkingLevel

from agent_comms.assignment_states import CompletedAssignment, FailedAssignment
from agent_comms.child_process import AttachedChild
from agent_comms.coordinated_runtime import SelectedExecution
from agent_comms.coordination_cohort import accept_delivery_cohort
from agent_comms.coordination_tables.attempts import ReplayFact
from agent_comms.coordinator import Coordination
from agent_comms.native_pi import NativePiTerminalFailure, NativePiUnavailable
from agent_comms.native_runtime_input import NativeRuntimeInput
from agent_comms.wake_candidate_index import (
    WakeCandidateIndex, RebuildIndex,
)
from agent_comms.wake_policy import PassiveWake
from compaction_loopback import LoopbackProvider
from test_coordinated_runtime import _root
from test_coordinated_runtime import tmp_path as private_root_fixture

tmp_path = private_root_fixture


class SelectedNativeJourney:
    def __init__(self, root, root_id, comms, package, direct):
        self.root, self.root_id, self.comms = root, root_id, comms
        self.package, self.direct = package, direct
        self.replies = deque()
        self.received = []
        self.children = []
        self.connections = set()
        self.session_file = None

    async def serve(self, reader, writer):
        task = asyncio.current_task()
        self.connections.add(task)
        try:
            reply = self.replies.popleft()
            self.received.append(reply)
            await reply.handle(reader, writer)
        finally:
            writer.close()
            await writer.wait_closed()
            self.connections.remove(task)

    def text(self, text):
        self.replies.append(LoopbackProvider(status=200, text=text))

    def execution(self):
        with closing(CursorDelivery()) as cursor_delivery:
            WakeCandidateIndex(self.comms.bus.log).maintain(start=RebuildIndex())
            return SelectedExecution(
                root=self.root,
                wire_root_id=self.root_id,
                owner_name="beta",
                native_package=self.package,
                session_file=self.session_file,
                cursor_delivery=cursor_delivery,
            )

    def send(self, text):
        message = publish_initial(self.comms, 
            "sender", "beta" if self.direct else "#team", text
        )
        with Coordination(str(self.root / "coordination.sqlite3")) as store:
            accept_delivery_cohort(self.comms.bus, self.root_id, message.seq, store)
        return message

    def records(self):
        with Coordination(str(self.root / "coordination.sqlite3")) as store:
            return NativeRuntimeInput.select(store.session._connection)

    async def seed(self):
        if not self.direct:
            self.text('{"decision":"FULL"}')
        self.text("Saved selected history before the failure")
        seed = self.execution()
        result = await seed.run()
        assert result.disposition is CompletedAssignment
        row = next(row for row in self.records() if row.input_id == result.input_id)
        self.session_file = Path(row.session_file)
        return row


