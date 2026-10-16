"""Source contention retains a viewer read, never manufactures an input failure."""

import asyncio
import json
import subprocess
import sys
from contextlib import contextmanager

import pytest

from agent_comms.acp import CommsAgent
from agent_comms.acp_extension import TranscriptSnapshotUpdate, decode_updates
from agent_comms.comms import wire
from agent_comms.coordinator import Coordination
from agent_comms.threads import Thread
from agent_comms.ui_model.observation import (
    ObservationProcess, ReadBusy, ReadNotifications, ReadTranscript,
)


def saved_history(tmp_path):
    comms = wire(tmp_path / "wire")
    session = tmp_path / "saved.jsonl"
    session.write_text(json.dumps({
        "type": "message", "id": "saved-answer", "parentId": None,
        "message": {"role": "assistant", "content": [{"type": "text", "text": "Saved answer"}]},
    }) + "\n")
    comms.registry.declare(Thread("saved", frozenset(), str(tmp_path), session_file=str(session)))
    with Coordination(str(comms.root / "coordination.sqlite3")) as store:
        store.install_private_runtime()
    return comms, session


@contextmanager
def held_source(comms):
    # A separate writer also keeps raw header inspection in the reader from
    # altering a same-process POSIX lock. No provider or user input is involved.
    child = subprocess.Popen([
        sys.executable, "-c",
        "import sqlite3,sys; db=sqlite3.connect(sys.argv[1]); "
        "db.execute('BEGIN EXCLUSIVE'); print('locked',flush=True); "
        "sys.stdin.readline(); db.rollback(); db.close()",
        str(comms.root / "coordination.sqlite3"),
    ], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
    try:
        assert child.stdout.readline().strip() == "locked"
        yield
    finally:
        child.stdin.write("release\n")
        child.stdin.flush()
        child.communicate(timeout=5)
        assert child.returncode == 0


async def test_busy_source_keeps_original_waiter_and_other_reads_move(tmp_path):
    comms, session = saved_history(tmp_path)
    original = session.read_bytes()
    with ObservationProcess.awaited(comms.route) as process:
        with held_source(comms):
            answer = ReadTranscript(str(comms.root), "saved", request_id=19).answer(comms)
            assert isinstance(answer, ReadBusy)
            assert answer.title == "History loading"
            waiting = asyncio.create_task(process.read(ReadTranscript(str(comms.root), "saved")))
            independent = asyncio.create_task(process.read(ReadNotifications(str(comms.root), ())))
            async with asyncio.timeout(15):
                assert await independent == {}
            assert not waiting.done()
            assert len(process.reads) == 1
            (request_id,) = process.reads
            original_waiter = process.reads[request_id]
            await asyncio.sleep(0.15)
            assert process.reads[request_id] is original_waiter
            # The writer leaves by rollback, without changing the revision.
        async with asyncio.timeout(5):
            snapshot = await waiting
        assert isinstance(snapshot, TranscriptSnapshotUpdate)
        assert [event.text for event in snapshot.page.events] == ["Saved answer"]
        assert snapshot.identity.session_file == str(session)
        assert process.reads == {}
    assert session.read_bytes() == original


async def test_startup_replay_uses_retained_read_not_delivery_error(tmp_path):
    comms, session = saved_history(tmp_path)
    original = session.read_bytes()
    agent = CommsAgent(comms)
    updates = []

    class Client:
        async def session_update(self, **kwargs):
            updates.append(kwargs["update"])

    task = None
    try:
        with held_source(comms):
            task = asyncio.create_task(agent.sessions.transcript.replay("saved", "saved", client=Client()))
            async with asyncio.timeout(15):
                while "reads" not in agent.sessions.transcript.__dict__:
                    await asyncio.sleep(0.01)
                process = agent.sessions.transcript.reads
                while not process.reads:
                    await asyncio.sleep(0.01)
            await asyncio.sleep(0.15)
            assert not task.done()
            assert updates == []  # Not an empty-history update or delivery/resend error.
        async with asyncio.timeout(5):
            await task
        snapshot, = (value for value in decode_updates(updates[0].field_meta)
                     if isinstance(value, TranscriptSnapshotUpdate))
        assert [event.text for event in snapshot.page.events] == ["Saved answer"]
        assert snapshot.identity.session_file == str(session)
        assert session.read_bytes() == original
    finally:
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        await agent.shutdown()


async def test_cancelled_busy_read_is_removed_without_resubmission(tmp_path):
    comms, _session = saved_history(tmp_path)
    with ObservationProcess.awaited(comms.route) as process:
        with held_source(comms):
            waiting = asyncio.create_task(process.read(ReadTranscript(str(comms.root), "saved")))
            await asyncio.sleep(0.15)
            waiting.cancel()
            with pytest.raises(asyncio.CancelledError):
                await waiting
            assert process.reads == {}
        async with asyncio.timeout(5):
            assert await process.read(ReadNotifications(str(comms.root), ())) == {}
        assert process.reads == {}
