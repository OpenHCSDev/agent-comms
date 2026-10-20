"""Single-owner attachment and idle wakeup through real processes and sockets."""

import asyncio
import json
import os
from dataclasses import replace

import pytest

from agent_comms.acp_extension import (
    CoordinationChangedUpdate,
    TranscriptSnapshotUpdate,
    decode_updates,
)
from agent_comms.child_process import ParentedProcess, ProcessIdentity
from agent_comms.comms import wire
from agent_comms.owner_protocol import OwnerAnnouncement, OwnerReady, OwnerResult, OwnerUpdate
from agent_comms.runtime import RuntimeProxy, present_session, socket_path
from agent_comms.thread_identity import RecordedThreadIncarnation
from delivery_owner_fixture import canonical_agent


@pytest.fixture
def runtime_processes():
    import sys

    children = []
    try:
        for _ in range(2):
            children.append(
                ParentedProcess.launch((sys.executable, "-c", "import time; time.sleep(60)"))
            )
        yield tuple(child.identity for child in children)
    finally:
        for child in children:
            child.stop_sync()


def test_owner_cursor_scope_rebases_only_attachment_session_alias():
    from agent_comms.acp_extension import (
        CursorAdvancedUpdate,
        CursorEnvelope,
        EmptyCursorObservation,
        LiveCursorScope,
        decode_updates,
        encode_updates,
    )
    from agent_comms.thread_identity import ClaimedIdentity

    fact = CursorAdvancedUpdate(
        CursorEnvelope(
            LiveCursorScope(
                "canonical",
                "a" * 32,
                ClaimedIdentity(RecordedThreadIncarnation("canonical", 1000.0), 3),
                1234,
            ),
            7,
            EmptyCursorObservation(),
        )
    )
    (rebased,) = decode_updates(present_session(encode_updates(fact), "old-alias"))
    assert rebased.envelope.scope == replace(fact.envelope.scope, session_id="old-alias")
    assert fact.envelope.scope.session_id == "canonical"
    assert rebased.envelope.revision == fact.envelope.revision


@pytest.mark.asyncio
@pytest.mark.parametrize("request_original", [False, True])
async def test_subscription_replay_uses_requested_alias(tmp_path, request_original):
    from agent_comms.coordinator import Coordination
    from agent_comms.launch_root import LaunchRoot

    comms = wire(tmp_path / "wire")
    LaunchRoot.from_path(comms.root).install()
    owner = canonical_agent(comms, runtime_enabled=True, auto_wake=False)
    client = canonical_agent(comms, auto_wake=False)
    updates = []

    class Client:
        async def session_update(self, session_id, update):
            updates.append((session_id, update))

    client.on_connect(Client())
    proxy = None
    try:
        response = await owner.new_session(str(tmp_path))
        original = response.session_id
        timestamp = "2026-10-20T00:00:00.000Z"
        journal = tmp_path / "history.jsonl"
        journal.write_text("".join(json.dumps(record) + "\n" for record in (
            {"type": "session", "version": 3, "id": "saved", "timestamp": timestamp},
            {"type": "message", "id": "answer", "parentId": None, "timestamp": timestamp,
             "message": {"role": "assistant", "content": [
                 {"type": "text", "text": "An original saved answer"}]}},
        )))
        thread = comms.registry.require(original)
        with Coordination(str(comms.root / "coordination.sqlite3")) as store:
            store.install_private_runtime()
            store.participants.register(thread.incarnation.lookup, original, original, committed=True)
        comms.threads.attach_session(thread, str(journal), process=thread.process_identity)
        comms.threads.rename_managed_thread(original, "renamed", owner_pid=os.getpid())
        requested = original if request_original else "renamed"
        proxy = RuntimeProxy(client, requested, socket_path(comms.root, os.getpid()))
        await proxy.subscribe()
        snapshots = [(session, fact) for session, update in updates
                     for fact in decode_updates(update.get("_meta"))
                     if isinstance(fact, TranscriptSnapshotUpdate)]
        assert len(snapshots) == 1
        session, snapshot = snapshots[0]
        assert session == requested == snapshot.identity.requested_name
        assert snapshot.identity.thread.incarnation == comms.registry.require("renamed").incarnation
        assert [event.text for event in snapshot.page.events] == ["An original saved answer"]
    finally:
        if proxy is not None:
            await proxy.close()
        await client.shutdown()
        await owner.shutdown()


async def until(predicate, timeout=10):
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(0.05)




@pytest.mark.asyncio
@pytest.mark.skipif(os.name == "nt", reason="POSIX socket runtime")
async def test_existing_proxy_follows_renamed_owner_restart_and_resubscribes(
    tmp_path, runtime_processes
):
    comms = wire(tmp_path / "wire")
    old, new = runtime_processes
    old_pid, new_pid = old.pid, new.pid
    comms.registry.declare(comms.registry.new_thread('worker', frozenset(), str(tmp_path), process_identity=old))
    client = canonical_agent(comms)
    updates = []

    class Client:
        async def session_update(self, *, session_id, update):
            updates.append((session_id, update))

    client.on_connect(Client())
    old_path = socket_path(comms.root, old_pid)
    new_path = socket_path(comms.root, new_pid)
    old_path.parent.mkdir(parents=True, exist_ok=True)
    connections = []
    calls = []

    def handler(owner):
        async def receive(reader, writer):
            request = json.loads(await reader.readline())
            calls.append((owner, request["action"], request["thread"]))
            if request["action"] == "subscribe":
                connections.append(writer)
                writer.write(OwnerAnnouncement().wire())
                writer.write(
                    OwnerReady(
                        ready={"agentComms": {"updates": []}},
                        config_options=[{"id": "model", "currentValue": owner}],
                        controller_token="a" * 64,
                    ).wire()
                )
                writer.write(OwnerUpdate(update={"owner": owner}).wire())
                await writer.drain()
                await reader.read()
            else:
                writer.write(OwnerResult(result={"owner": owner}).wire())
                await writer.drain()
            writer.close()

        return receive

    old_server = await asyncio.start_unix_server(handler("old"), path=old_path)
    new_server = None
    proxy = RuntimeProxy(client, "worker", old_path)
    try:
        await proxy.subscribe()
        await until(
            lambda: any(
                session == "worker" and data.get("owner") == "old" for session, data in updates
            )
        )
        # Session metadata may be filled in after initial owner attachment.
        comms.registry.register(
            replace(comms.registry.require("worker"), session_file=str(tmp_path / "session.jsonl"))
        )
        assert await proxy.request("cancel") == {"owner": "old"}
        comms.registry.rename("worker", "renamed")
        comms.registry.register(replace(comms.registry.require("renamed"), process_identity=new))
        old_server.close()
        for writer in connections:
            writer.close()
        await old_server.wait_closed()

        # A request made before the replacement socket starts is still unsent.
        request = asyncio.create_task(proxy.request("cancel"))
        await asyncio.sleep(0.15)
        new_server = await asyncio.start_unix_server(handler("new"), path=new_path)
        assert await asyncio.wait_for(request, 3) == {"owner": "new"}
        await until(
            lambda: any(
                session == "worker" and data.get("owner") == "new" for session, data in updates
            )
        )
        await until(
            lambda: (
                (
                    "worker",
                    {
                        "sessionUpdate": "config_option_update",
                        "configOptions": [{"id": "model", "currentValue": "new"}],
                    },
                )
                in updates
            )
        )
        assert ("new", "subscribe", "worker") in calls
        assert ("new", "cancel", "worker") in calls
        assert calls.count(("old", "cancel", "worker")) == 1

        # A later thread with the same former name is a distinct incarnation.
        comms.registry.unregister("renamed")
        comms.registry.begin_delete("renamed")
        comms.registry.remove("renamed")
        comms.registry.declare(
            comms.registry.new_thread('worker', frozenset(), str(tmp_path), process_identity=ProcessIdentity(901003, 1))
        )
        with pytest.raises(RuntimeError):
            await proxy.request("cancel")
    finally:
        await proxy.close()
        await client.shutdown()
        old_server.close()
        if new_server is not None:
            new_server.close()
        for writer in connections:
            writer.close()
        await old_server.wait_closed()
        if new_server is not None:
            await new_server.wait_closed()
        old_path.unlink(missing_ok=True)
        new_path.unlink(missing_ok=True)


@pytest.mark.asyncio
@pytest.mark.skipif(os.name == "nt", reason="POSIX socket runtime")
async def test_subscriber_receives_identity_before_transcript_replay(tmp_path):
    comms = wire(tmp_path / "wire")
    owner = canonical_agent(comms, agent_bin="/bin/echo", agent_args=[], runtime_enabled=True)
    client = canonical_agent(comms)
    updates = []

    class Client:
        async def session_update(self, session_id, update):
            updates.append(update)

    client.on_connect(Client())
    response = await owner.new_session(str(tmp_path))
    entered, release = asyncio.Event(), asyncio.Event()

    async def delayed_replay(*args, **kwargs):
        entered.set()
        await release.wait()

    owner.sessions.transcript.replay = delayed_replay
    proxy = RuntimeProxy(client, response.session_id, socket_path(comms.root, os.getpid()))
    task = asyncio.create_task(proxy.subscribe())
    try:
        await asyncio.wait_for(entered.wait(), 2)
        await until(
            lambda: any(
                any(
                    isinstance(f, CoordinationChangedUpdate)
                    and f.wire_root == str(comms.root.resolve())
                    for f in decode_updates(update.get("_meta"))
                )
                for update in updates
            ),
            timeout=2,
        )
        assert not task.done()
        release.set()
        await asyncio.wait_for(task, 2)
    finally:
        release.set()
        await proxy.close()
        await owner.shutdown()




@pytest.mark.asyncio
async def test_new_session_metadata_has_a_display_title_before_first_switch(tmp_path):
    comms = wire(tmp_path / "wire")
    agent = canonical_agent(comms, agent_bin="/bin/echo")
    try:
        response = await agent.new_session(str(tmp_path / "project"))
        assert (
            next(
                f
                for f in decode_updates(response.field_meta)
                if isinstance(f, CoordinationChangedUpdate)
            ).title
            == "project"
        )
    finally:
        await agent.shutdown()






