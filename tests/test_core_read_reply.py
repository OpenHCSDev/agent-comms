"""A malformed child reply releases the real worker that asked for it."""

import asyncio
from dataclasses import replace
from queue import SimpleQueue
from threading import Event, Thread

from agent_comms.comms import wire
from agent_comms.nominal.field_codec import MalformedRecord
from agent_comms.transport.tools.sidebar import CommsChannelTool
from agent_comms.ui_model.observation import (
    CoreReadAnswered,
    CoreReadOperation,
    ObservationProcess,
)


async def test_malformed_reply_releases_actual_waiting_worker(tmp_path, monkeypatch):
    comms = wire(tmp_path / "wire")
    comms.channels.create_tag("test-reply")
    original_received = CoreReadAnswered.received
    reply_received = Event()
    worker_finished = Event()
    errors = SimpleQueue()

    async def corrupt_reply(answer, process, observed):
        reply_received.set()
        await original_received(replace(answer, wire={"not": "a channel"}), process, observed)

    monkeypatch.setattr(CoreReadAnswered, "received", corrupt_reply)

    async with ObservationProcess.awaited(comms.route) as process:
        request = CoreReadOperation(str(comms.root), CommsChannelTool(name="#test-reply"))

        def ask():
            try:
                process.read_from_thread(request)
            except BaseException as error:
                errors.put(error)
            finally:
                worker_finished.set()

        worker = Thread(target=ask, daemon=True)
        worker.start()
        assert await asyncio.to_thread(reply_received.wait, 15), "the child never answered"
        assert await asyncio.to_thread(worker_finished.wait, 5), "malformed reply stranded its worker"
        await asyncio.to_thread(worker.join, 5)
        assert not worker.is_alive()
        assert isinstance(errors.get_nowait(), MalformedRecord)
        assert process.reads == {}

    assert process.peer.process.exitcode == 0
