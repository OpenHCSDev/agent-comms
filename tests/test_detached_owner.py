"""The first UI is an attachment too: disconnecting cannot cancel its running turn."""

import asyncio
import os
import sys
from contextlib import suppress

import pytest

from agent_comms.acp import CommsClient
from agent_comms.comms import wire


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
        assert comms.registry.require(name).process_alive and comms.registry.require(name).executing
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
        assert comms.registry.require(name).process_alive
        assert comms.registry.require(name).pid == owner
    finally:
        turn.cancel()
        await asyncio.gather(turn, return_exceptions=True)
        await first.shutdown()
        await second.shutdown()
        await third.shutdown()
        comms.owners.stop(name)
        with suppress(ChildProcessError):
            await asyncio.to_thread(os.waitpid, owner, 0)
