"""Single-owner attachment and idle wakeup through real processes and sockets."""

import asyncio
import os

import pytest

from agent_comms import ForkSpec, Thread
from agent_comms.acp import CommsAgent
from agent_comms.operations import wire
from agent_comms.runtime import socket_path


async def until(predicate, timeout=10):
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(0.05)


@pytest.mark.asyncio
async def test_fork_owner_survives_turn_and_two_clients_attach_without_duplicate(tmp_path):
    comms = wire(tmp_path / "wire")
    session = tmp_path / "parent.jsonl"
    session.write_text("")
    comms.register(
        Thread(name="parent", tags=frozenset(), worktree=str(tmp_path), session_file=str(session))
    )
    comms.send("parent", "#all", "old broadcast must not be delivered")
    child = comms.fork(
        ForkSpec(name="child", parent="parent", task="initial turn"), pi_bin="/bin/echo"
    )
    first, second = CommsAgent(comms), CommsAgent(comms)
    first_updates, second_updates = [], []

    class Client:
        def __init__(self, updates):
            self.updates = updates

        async def session_update(self, session_id, update):
            self.updates.append(
                update
                if isinstance(update, dict)
                else update.model_dump(by_alias=True, exclude_none=True)
            )

    first.on_connect(Client(first_updates))
    second.on_connect(Client(second_updates))
    try:
        await until(lambda: socket_path(comms.root, child.pid).exists())
        await until(
            lambda: any(
                m.sender == "child" and "initial turn" in m.body
                for m in comms.channel_history("#all")
            )
        )
        assert comms.registry.require("child").pid == child.pid
        assert comms.registry.status("child").value == "running"
        assert comms.activity_of("child").state.value == "idle"
        for agent in (first, second):
            response = await agent.load_session(str(tmp_path), "child")
            assert response.field_meta["agentComms"]["ownerPid"] == child.pid
        assert comms.registry.require("child").pid == child.pid
        comms.send("parent", "child", "second round without polling or sleeping")
        comms.acknowledge("child")  # Reading in a UI must not eat the agent's delivery.
        await until(
            lambda: any(
                m.sender == "child" and "second round" in m.body
                for m in comms.channel_history("#all")
            )
        )
        for updates in (first_updates, second_updates):
            incoming = [
                u["_meta"]["agentComms"]["incoming"]
                for u in updates
                if "incoming" in u.get("_meta", {}).get("agentComms", {})
            ]
            assert len(incoming) == 1
            assert incoming[0]["sender"] == "parent"
            assert "second round" in incoming[0]["body"]
        responses = [
            m
            for m in comms.channel_history("#all")
            if m.sender == "child" and "second round" in m.body
        ]
        assert len(responses) == 1
        assert "old broadcast must not be delivered" not in responses[0].body
        # A UI prompt is forwarded to that same owner, not run by either client.
        response = await second.prompt("child", [{"type": "text", "text": "third round"}])
        assert response.stop_reason == "end_turn"
        assert comms.registry.require("child").pid == child.pid
        await first.shutdown()
        assert comms._process_alive(child.pid)
        assert comms.registry.status("child").value == "running"
        comms.rename_managed_thread("child", "renamed child", owner_pid=child.pid)
        response = await second.prompt("child", [{"type": "text", "text": "after rename"}])
        assert response.stop_reason == "end_turn"
        assert comms.registry.require("child").name == "renamed-child"
        assert len(comms.registry.all_threads()) == 2
        comms.stop("parent")
        deleted = comms.delete("parent")
        assert deleted.detached_children == ("renamed-child",)
        assert comms.registry.require("child").parent is None
        assert comms.registry.require("child").pid == child.pid
        response = await second.prompt("child", [{"type": "text", "text": "after parent deletion"}])
        assert response.stop_reason == "end_turn"
        assert comms._process_alive(child.pid)
    finally:
        await first.shutdown()
        await second.shutdown()
        await asyncio.to_thread(comms.stop, "child")
        # Reap the child started by fork (otherwise /proc retains a zombie).
        await asyncio.to_thread(os.waitpid, child.pid, 0)
    comms.delete("child")
    assert "child" not in comms.registry
    assert "renamed-child" not in comms.registry


def test_fork_rejects_duplicate_instead_of_overwriting_owner(tmp_path):
    from agent_comms import RelationViolationError

    comms = wire(tmp_path)
    session = tmp_path / "parent.jsonl"
    session.write_text("")
    comms.register(
        Thread(name="parent", tags=frozenset(), worktree=str(tmp_path), session_file=str(session))
    )
    comms.register(Thread(name="child", tags=frozenset(), worktree=str(tmp_path), pid=os.getpid()))
    with pytest.raises(RelationViolationError, match="already exists"):
        comms.fork(ForkSpec(name="child", parent="parent", task="duplicate"))
    assert comms.registry.require("child").pid == os.getpid()
