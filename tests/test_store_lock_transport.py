"""A busy store stays a bounded pending read across the observer's process pipe."""

import asyncio
import errno
import io
import multiprocessing
import pickle

import pytest

from agent_comms.coordination.errors import ContendedRead
from agent_comms.registry.registry_store import RegistryStore
from agent_comms.storage.store_files import StoreGuard, StoreLockBusyError, StoreLockContention
from agent_comms.transport.tools.sidebar import CommsRegistryTool
from agent_comms.ui_model.observation import (
    CoreReadAnswered, CoreReadOperation, ObservationService, ReadBusy, ReadRefused,
)
from test_locked_store import _hold_store_write, event, process


def test_real_lock_failure_keeps_holders_and_cause_across_process_pipe(comms):
    registry = comms.registry.store
    ready, release = event(), event()
    with process(_hold_store_write, RegistryStore, registry.path, ready, release) as writer:
        try:
            assert ready.wait(5)
            with pytest.raises(StoreLockBusyError) as caught, registry.read_now():
                pytest.fail("The reader crossed the held writer")
            failure = caught.value
            assert f"PID {writer.pid}: " in str(failure)
            encoded = pickle.dumps(failure)
            copies = (pickle.loads(encoded), pickle._Unpickler(io.BytesIO(encoded)).load())
            reader, sender = multiprocessing.Pipe(duplex=False)
            with reader, sender:
                sender.send(ReadRefused(7, failure, "original store refusal"))
                copies += (reader.recv().error,)
                sender.send("observer still readable")
                assert reader.recv() == "observer still readable"
            for copy in copies:
                assert type(copy) is StoreLockBusyError
                assert isinstance(copy, BlockingIOError)
                assert isinstance(copy, ContendedRead)
                assert copy.errno == errno.EAGAIN
                assert copy.args == failure.args
                assert copy.strerror == failure.strerror
                assert copy.holders == failure.holders
                assert str(copy) == str(failure)
                assert type(copy.__cause__) is BlockingIOError
                assert copy.__cause__.errno == errno.EAGAIN
        finally:
            release.set()


async def test_observer_registry_read_stays_pending_until_writer_releases(comms, monkeypatch):
    registry = comms.registry.store
    before = registry.snapshot()
    request = CoreReadOperation(str(comms.root), CommsRegistryTool(), request_id=19)
    ready, release = event(), event()
    monkeypatch.setattr(StoreGuard, "contention", StoreLockContention(.025))
    service_end, viewer = multiprocessing.Pipe()
    with service_end, viewer:
        service = ObservationService(comms, service_end)
        try:
            with process(_hold_store_write, RegistryStore, registry.path, ready, release):
                try:
                    assert ready.wait(5)
                    viewer.send(request)
                    receiving = asyncio.create_task(asyncio.to_thread(viewer.recv))
                    await service.answer_pending(set())
                    answer = await receiving
                    assert isinstance(answer, ReadBusy)
                    assert answer.request_id == request.request_id
                    assert service.pending[request.request_id] == request
                finally:
                    release.set()
            receiving = asyncio.create_task(asyncio.to_thread(viewer.recv))
            await service.answer_pending(set())
            answered = await receiving
            assert isinstance(answered, CoreReadAnswered)
            assert answered.request_id == request.request_id
            assert request.request_id not in service.pending
            assert not service.closed
            assert registry.snapshot() == before
        finally:
            service.retire()
