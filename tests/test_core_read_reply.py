"""A malformed child reply releases the real worker that asked for it."""

import asyncio
from dataclasses import replace
from queue import SimpleQueue
from threading import Event, Thread

from agent_comms.comms import wire
from agent_comms.nominal.field_codec import MalformedRecord
from agent_comms.transport.tools.sidebar import CommsChannelTool
import pytest

from agent_comms.ui_model.observation import (
    CoreReadAnswered,
    CoreReadOperation,
    LocalRouteObservation,
    ObservationProcess,
    ObserverUnattached,
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


def asked_from_a_worker(process: ObservationProcess, request: CoreReadOperation):
    """One worker thread's read, with whatever it ended in."""
    outcome = SimpleQueue()

    def ask():
        try:
            outcome.put(process.read_from_thread(request))
        except BaseException as error:
            outcome.put(error)

    worker = Thread(target=ask, daemon=True)
    worker.start()
    return worker, outcome


async def test_a_read_asked_before_attachment_waits_for_it(tmp_path):
    """On 2026-11-10 Toad made an observation current, scheduled its attachment, and its tab
    restore read from a worker in between: the launch failed with ObserverUnattached."""
    comms = wire(tmp_path / "wire")
    comms.channels.create_tag("early-read")
    async with ObservationProcess(LocalRouteObservation(comms.route)) as process:
        request = CoreReadOperation(str(comms.root), CommsChannelTool(name="#early-read"))
        worker, outcome = asked_from_a_worker(process, request)
        await asyncio.sleep(0.2)
        assert worker.is_alive(), "the early read refused instead of waiting"
        process.start_receiving(process.observed, process.failed)
        await asyncio.to_thread(worker.join, 15)
        answered = outcome.get_nowait()
        assert not isinstance(answered, BaseException), answered


async def test_a_read_with_no_attachment_coming_still_refuses_by_name(tmp_path, monkeypatch):
    monkeypatch.setattr(ObservationProcess, "attach_seconds", 0.1)
    comms = wire(tmp_path / "wire")
    async with ObservationProcess(LocalRouteObservation(comms.route)) as process:
        request = CoreReadOperation(str(comms.root), CommsChannelTool(name="#never"))
        worker, outcome = asked_from_a_worker(process, request)
        await asyncio.to_thread(worker.join, 5)
        with pytest.raises(ObserverUnattached, match="attached no reader"):
            raise outcome.get_nowait()


async def test_a_thread_presentation_read_decodes_across_the_child(tmp_path):
    """Core serves a thread presentation as the published NotifiedThreadPresentation. On
    2026-11-10 the read declared the parent ThreadPresentation, so the reply's notifications and
    read_identity were refused as unknown fields on every local Toad read."""
    from agent_comms.projects.checkouts import Checkout
    from agent_comms.registry.threads import NativeThread
    from agent_comms.transcript.transcripts import NotifiedThreadPresentation
    from agent_comms.transport.tools.sidebar import CommsThreadPresentationTool

    comms = wire(tmp_path / "wire")
    comms.registry.declare(NativeThread("presented", frozenset(), Checkout(tmp_path)))
    async with ObservationProcess.awaited(comms.route) as process:
        request = CoreReadOperation(str(comms.root), CommsThreadPresentationTool(thread="presented"))
        presentation = await asyncio.to_thread(process.read_from_thread, request)
    assert isinstance(presentation, NotifiedThreadPresentation)


async def test_an_unencodable_reply_fails_its_own_read_and_the_child_keeps_answering(tmp_path):
    """On 2026-11-10 Toad asked a registry slice with an AcpSessionId among its thread names; the
    reply's str-typed ``asked`` could not be encoded, and the whole child died with every UI
    read in flight. The caller gets the failure now, and the next read is still answered."""
    from agent_comms.nominal.field_codec import AcpSessionId
    from agent_comms.projects.checkouts import Checkout
    from agent_comms.registry.threads import NativeThread
    from agent_comms.transport.tools.sidebar import CommsRegistrySliceTool
    from agent_comms.ui_model.observation import UnencodableReply

    comms = wire(tmp_path / "wire")
    comms.registry.declare(NativeThread("sliced", frozenset(), Checkout(tmp_path)))
    async with ObservationProcess.awaited(comms.route) as process:
        bad = CoreReadOperation(
            str(comms.root), CommsRegistrySliceTool(threads=(AcpSessionId("sliced"),)))
        with pytest.raises(UnencodableReply, match="CommsRegistrySliceTool answered a RegistrySlice"):
            await asyncio.to_thread(process.read_from_thread, bad)
        good = CoreReadOperation(str(comms.root), CommsRegistrySliceTool(threads=("sliced",)))
        sliced = await asyncio.to_thread(process.read_from_thread, good)
    assert "sliced" in sliced.threads
