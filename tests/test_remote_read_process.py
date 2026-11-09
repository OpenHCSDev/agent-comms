"""Real child execution, typed decoding, progress and joined shutdown."""

import asyncio
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
import os
from pathlib import Path
import time

import pytest
from yarl import URL

from agent_comms.connector_profile import BearerSecret, HttpsMcpEndpoint, UiCredential, UiGrantId
from agent_comms.nominal.field_codec import RootId
from agent_comms.remote_core_process import ProcessRemoteCoreAccess
from agent_comms.tool_result import ToolResult
from agent_comms.transport.progress import WireExportObserver, WireExportProgress
from agent_comms.transport.request import ObservedRequest, ReadRequest, ToolRequest


@dataclass(frozen=True)
class CpuReply(ToolResult):
    produced_pid: int
    checksum: int
    decoded_pid: int = 0

    def __post_init__(self):
        object.__setattr__(self, "decoded_pid", os.getpid())


@dataclass(frozen=True)
class CpuRead(ReadRequest[CpuReply], declared_name="test_process_cpu_read"):
    seconds: float = .05

    def posted(self, remote):
        started, value = time.thread_time(), 1
        while time.thread_time() - started < self.seconds:
            value = (value * 1664525 + 1013904223) & 0xffffffff
        return {"produced_pid": os.getpid(), "checksum": value}


@dataclass(frozen=True)
class FailedRead(ReadRequest[CpuReply], declared_name="test_process_failed_read"):
    def posted(self, remote):
        raise ValueError("remote read failed")


_writes = []


@dataclass(frozen=True)
class FailedWrite(ToolRequest[CpuReply], declared_name="test_process_failed_write"):
    def posted(self, remote):
        _writes.append(os.getpid())
        raise RuntimeError("the write reply was lost")


@dataclass(frozen=True, kw_only=True)
class ProgressCall(ObservedRequest[CpuReply], declared_name="test_process_progress_call"):
    def posted(self, remote):
        self.observer.advanced(WireExportProgress(12, 1))
        return {"produced_pid": os.getpid(), "checksum": 12}


class Progress(WireExportObserver):
    def __init__(self):
        self.updates = []

    def advanced(self, progress):
        self.updates.append(progress)


@pytest.fixture
def access():
    client = ProcessRemoteCoreAccess(
        HttpsMcpEndpoint(URL("https://unused.example/mcp")),
        UiCredential(UiGrantId("1" * 32), RootId("a" * 32), 2**31, BearerSecret("test")))
    yield client
    client.close()


def test_read_and_typed_decode_run_outside_the_ui_while_it_keeps_ticking(access):
    async def exercise():
        pending = asyncio.create_task(asyncio.to_thread(access.call, CpuRead(seconds=.3)))
        ticks = 0
        while not pending.done():
            await asyncio.sleep(.01)
            ticks += 1
        reply = await pending
        assert ticks >= 10
        assert reply.produced_pid != os.getpid()
        assert reply.decoded_pid == reply.produced_pid
        return reply.produced_pid

    child = asyncio.run(exercise())
    assert Path(f"/proc/{child}").exists()
    access.close()
    assert not Path(f"/proc/{child}").exists()


def test_concurrent_reads_share_the_owned_bounded_processes(access):
    with ThreadPoolExecutor(max_workers=6) as callers:
        replies = list(callers.map(access.call, [CpuRead()] * 6))
    pids = {reply.produced_pid for reply in replies}
    assert 1 <= len(pids) <= 4
    assert os.getpid() not in pids
    assert all(reply.decoded_pid == reply.produced_pid for reply in replies)


def test_a_failed_read_remains_a_failure_and_does_not_retire_the_client(access):
    with pytest.raises(ValueError, match="remote read failed"):
        access.call(FailedRead())
    assert access.call(CpuRead()).produced_pid != os.getpid()


def test_a_write_is_not_sent_to_a_child_or_replayed(access):
    _writes.clear()
    with pytest.raises(RuntimeError, match="write reply was lost"):
        access.call(FailedWrite())
    assert _writes == [os.getpid()]


def test_progress_keeps_its_actual_observer(access):
    observer = Progress()
    reply = access.call(ProgressCall(observer=observer))
    assert reply.produced_pid == os.getpid()
    assert observer.updates == [WireExportProgress(12, 1)]


def test_a_closed_client_refuses_a_new_read(access):
    access.close()
    with pytest.raises(RuntimeError, match="shutdown"):
        access.call(CpuRead())
