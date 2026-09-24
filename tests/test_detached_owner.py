"""The first UI is an attachment too: disconnecting cannot cancel its running turn."""

import asyncio
import os
import sys
from contextlib import suppress
from threading import Thread as WorkerThread

import pytest

from agent_comms import Thread, wire
from agent_comms.acp import CommsClient


@pytest.mark.skipif(os.name == "nt", reason="Named FIFO fixture requires POSIX")
async def test_new_thread_survives_client_loss_and_reattaches_without_duplicate(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    gate = tmp_path / "gate"
    os.mkfifo(gate)
    program = tmp_path / "slow backend.py"
    program.write_text(
        "print('TURN_STARTED', flush=True)\n"
        f"with open({str(gate)!r}, 'rb') as gate: gate.read(1)\n"
        "print('TURN_FINISHED', flush=True)\n"
    )
    comms = wire(tmp_path / "wire")
    first = CommsClient(comms, agent_bin=sys.executable, agent_args=[str(program)])
    second, third = CommsClient(comms), CommsClient(comms)
    started, settled = asyncio.Event(), asyncio.Event()

    class Client:
        async def session_update(self, session_id, update):
            if "TURN_STARTED" in update.get("content", {}).get("text", ""):
                started.set()
            if update.get("_meta", {}).get("agentComms", {}).get("turnSettled"):
                settled.set()

    first.on_connect(Client())
    second.on_connect(Client())
    response = await first.new_session(str(project))
    name = response.session_id
    owner = comms.registry.require(name).pid
    assert owner != os.getpid() and os.getpgid(owner) == owner
    turn = asyncio.create_task(first.prompt(name, [{"type": "text", "text": "work"}]))
    try:
        await asyncio.wait_for(started.wait(), 10)
        await first.shutdown()
        turn.cancel()
        await asyncio.gather(turn, return_exceptions=True)
        assert comms._process_alive(owner) and comms.registry.require(name).executing
        attachments = await asyncio.gather(
            second.load_session(str(project), name), third.load_session(str(project), name)
        )
        assert all(item.field_meta["agentComms"]["ownerPid"] == owner for item in attachments)
        settled.clear()  # Attachment's turn state precedes the completion we await.
        await asyncio.to_thread(gate.write_bytes, b"g")
        await asyncio.wait_for(settled.wait(), 10)
        assert not comms.registry.require(name).executing
        await second.shutdown()
        await third.shutdown()
        assert comms._process_alive(owner)
        assert comms.registry.require(name).pid == owner
    finally:
        turn.cancel()
        await asyncio.gather(turn, return_exceptions=True)
        await first.shutdown()
        await second.shutdown()
        await third.shutdown()
        comms.stop(name)
        with suppress(ChildProcessError):
            await asyncio.to_thread(os.waitpid, owner, 0)


def test_owner_launch_reservation_is_shared_and_never_adopts_a_ui(tmp_path, monkeypatch):
    comms = wire(tmp_path)
    comms.register(Thread("worker", frozenset(), str(tmp_path)))
    launches = []

    class Process:
        pid = 424242

        def __init__(self, command, **kwargs):
            launches.append((command, kwargs))
            if kwargs["pass_fds"]:
                inherited = os.dup(kwargs["pass_fds"][0])

                def accept_reservation():
                    try:
                        os.read(inherited, 1024)
                    finally:
                        os.close(inherited)

                WorkerThread(target=accept_reservation, daemon=True).start()

    monkeypatch.setattr("agent_comms.operations.subprocess.Popen", Process)
    monkeypatch.setattr(type(comms), "_process_alive", staticmethod(lambda pid: pid == Process.pid))
    monkeypatch.setattr(type(comms), "_is_local_participant", lambda self, thread, wait=True: True)
    one = comms.ensure_owner("worker", agent_args=["a value with spaces"])
    two = wire(tmp_path).ensure_owner("worker")
    assert one.pid == two.pid == Process.pid and len(launches) == 1
    assert launches[0][1]["start_new_session"]
    assert launches[0][1]["env"]["AGENT_COMMS_AGENT_ARGS"] == "'a value with spaces'"
