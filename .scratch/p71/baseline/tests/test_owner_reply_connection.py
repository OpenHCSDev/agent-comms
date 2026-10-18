"""Owner reads bound the dispatched reply, not local connection preparation."""

import asyncio
import json
import os

from agent_comms.child_process import ProcessIdentity
from agent_comms.comms import Comms
from agent_comms.runtime import OwnerReplyConnection
from agent_comms.threads import Thread
from agent_comms.ui_model.observation import OwnerRead, ReadAnswered, ReadRefused


class GoalRead(OwnerRead):
    async def read(self, owner):
        return await owner.request("goal_snapshot")


class Writer:
    def __init__(self):
        self.written = bytearray()

    def write(self, data):
        self.written.extend(data)

    async def drain(self):
        pass

    def close(self):
        pass

    async def wait_closed(self):
        pass


def owner_read(tmp_path):
    comms = Comms(tmp_path)
    thread = Thread(
        "worker", frozenset(), str(tmp_path),
        process_identity=ProcessIdentity.capture(os.getpid()),
    )
    comms.registry.declare(thread)
    return comms, GoalRead(str(tmp_path), thread.incarnation)


async def test_slow_connection_preparation_does_not_consume_reply_timeout(tmp_path, monkeypatch):
    comms, request = owner_read(tmp_path)
    writer = Writer()

    async def connect(self):
        await asyncio.sleep(0.03)
        reader = asyncio.StreamReader()
        reader.feed_data(json.dumps({"result": {"goal": None}}).encode() + b"\n")
        reader.feed_eof()
        return reader, writer

    monkeypatch.setattr(OwnerReplyConnection, "REPLY_TIMEOUT", 0.01)
    monkeypatch.setattr(OwnerReplyConnection, "_connect_current", connect)
    answer = await request.answer(comms)
    assert isinstance(answer, ReadAnswered)
    assert answer.value == {"goal": None}
    assert json.loads(writer.written) == {"action": "goal_snapshot", "thread": "worker"}


async def test_dispatched_owner_reply_remains_bounded(tmp_path, monkeypatch):
    comms, request = owner_read(tmp_path)
    writer = Writer()

    async def connect(self):
        return asyncio.StreamReader(), writer

    monkeypatch.setattr(OwnerReplyConnection, "REPLY_TIMEOUT", 0.01)
    monkeypatch.setattr(OwnerReplyConnection, "_connect_current", connect)
    answer = await request.answer(comms)
    assert isinstance(answer, ReadRefused)
    assert isinstance(answer.error, TimeoutError)
    assert json.loads(writer.written) == {"action": "goal_snapshot", "thread": "worker"}
