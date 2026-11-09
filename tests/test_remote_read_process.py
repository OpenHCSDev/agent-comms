"""Real child execution, typed decoding, progress and joined shutdown."""

import asyncio
import os
import socket
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from multiprocessing.connection import Connection
from pathlib import Path

import pytest
from yarl import URL

from agent_comms.execution.remote_core_process import ProcessRemoteCoreAccess, ProcessViewerEnd
from agent_comms.nominal.field_codec import FieldCodec, RootId
from agent_comms.transport.connector_profile import (
    BearerSecret,
    HttpsMcpEndpoint,
    UiCredential,
    UiGrantId,
)
from agent_comms.transport.progress import WireExportObserver, WireExportProgress
from agent_comms.transport.request import ObservedRequest, ReadRequest, ToolRequest
from agent_comms.transport.tool_result import ToolResult


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


@dataclass(frozen=True)
class HeldRead(ReadRequest[CpuReply], declared_name="test_process_held_read"):
    release: Path

    def posted(self, remote):
        self.release.with_name(f"read-{os.getpid()}.ready").touch()
        deadline = time.monotonic() + 45
        while not self.release.exists():
            if time.monotonic() >= deadline:
                raise TimeoutError("The test did not release its read")
            time.sleep(.01)
        return {"produced_pid": os.getpid(), "checksum": 1}


class ProbedViewerEnd(ProcessViewerEnd):
    receives = CpuReply


def reply_packet():
    return FieldCodec.compact(FieldCodec.encode_document(
        CpuReply(os.getpid(), 42), CpuReply)).encode()


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


def test_received_packets_use_their_declared_decoder_in_a_child(access):
    incoming, outgoing = socket.socketpair()
    with ProbedViewerEnd(incoming.detach(), access.read_process) as viewer, Connection(outgoing.detach()) as sender:
        sender.send_bytes(reply_packet())
        reply = viewer.recv()
        assert reply.checksum == 42
        assert reply.decoded_pid != os.getpid()


def test_malformed_packets_fail_and_the_next_packet_can_still_be_decoded(access):
    incoming, outgoing = socket.socketpair()
    with ProbedViewerEnd(incoming.detach(), access.read_process) as viewer, Connection(outgoing.detach()) as sender:
        sender.send_bytes(b"not JSON")
        with pytest.raises(ValueError):
            viewer.recv()
        sender.send_bytes(reply_packet())
        assert viewer.recv().checksum == 42


def test_slow_reads_leave_an_interpreter_available_for_arrived_packets(access, tmp_path):
    release = tmp_path / "release"
    with ThreadPoolExecutor(max_workers=5) as callers:
        reads = [callers.submit(access.call, HeldRead(release)) for _ in range(4)]
        try:
            deadline = time.monotonic() + 30
            while len(tuple(tmp_path.glob("read-*.ready"))) < 3:
                assert time.monotonic() < deadline, "Read workers did not start"
                time.sleep(.01)
            decoded = callers.submit(access.read_process.decode, ProbedViewerEnd.decode, reply_packet())
            assert decoded.result(timeout=30).decoded_pid != os.getpid()
            assert all(not read.done() for read in reads)
        finally:
            release.touch()
        assert all(read.result(timeout=30).produced_pid != os.getpid() for read in reads)
