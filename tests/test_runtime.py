"""Single-owner attachment and idle wakeup through real processes and sockets."""

import asyncio
import json
import os
from contextlib import suppress
from dataclasses import replace

import pytest

from agent_comms import ForkSpec, Thread
from agent_comms.acp import CommsAgent
from agent_comms.operations import wire
from agent_comms.runtime import RuntimeProxy, _present_cursor_session, socket_path


def test_owner_cursor_scope_rebases_only_attachment_session_alias():
    scope = {
        "sessionId": "canonical",
        "wireRootId": "a" * 32,
        "ownerThread": "canonical",
        "ownerCreatedAt": 1000.0,
        "ownerPid": 1234,
        "ownerEpoch": 3,
    }
    for field in ("agentComms", "_meta"):
        cursor = {"version": 1, "scope": dict(scope), "revision": 7, "status": "none"}
        metadata = (
            {"agentComms": {"privateNativeCursor": cursor}}
            if field == "agentComms"
            else {"_meta": {"agentComms": {"privateNativeCursor": cursor}}}
        )
        assert _present_cursor_session(metadata, "old-alias") is metadata
        assert cursor == {
            "version": 1,
            "scope": {**scope, "sessionId": "old-alias"},
            "revision": 7,
            "status": "none",
        }
    unsupported = {"agentComms": {"privateNativeCursor": {"version": 2, "scope": dict(scope)}}}
    _present_cursor_session(unsupported, "old-alias")
    assert unsupported["agentComms"]["privateNativeCursor"]["scope"] == scope


async def until(predicate, timeout=10):
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(0.05)


@pytest.mark.asyncio
@pytest.mark.skipif(os.name == "nt", reason="POSIX socket runtime")
async def test_owner_prompt_rejection_preserves_reason_and_rpc_code(tmp_path):
    from acp.exceptions import RequestError

    comms = wire(tmp_path / "wire")
    owner = CommsAgent(comms, agent_bin="/bin/echo", agent_args=[], runtime_enabled=True)
    client = CommsAgent(comms)
    response = await owner.new_session(str(tmp_path / "project"))
    proxy = RuntimeProxy(client, response.session_id, socket_path(comms.root, os.getpid()))
    request = {
        "prompt": [{"type": "text", "text": "Do not launch"}],
        "meta": {"agentComms": {"delivery": "invalid"}},
    }
    try:
        with pytest.raises(RequestError) as caught:
            await proxy.request("prompt", **request)
        assert caught.value.code == -32602
        assert caught.value.data == {"reason": "delivery must be queue or steer"}
        # The older, already-running proxy reads only the string field.
        reader, writer = await asyncio.open_unix_connection(proxy.path)
        try:
            writer.write(
                (
                    json.dumps({"action": "prompt", "thread": response.session_id, **request})
                    + "\n"
                ).encode()
            )
            await writer.drain()
            result = json.loads(await reader.readline())
            assert result["error"] == "delivery must be queue or steer"
        finally:
            writer.close()
            await writer.wait_closed()
        assert comms.registry.require(response.session_id).active_turn is None
    finally:
        await proxy.close()
        await client.shutdown()
        await owner.shutdown()


@pytest.mark.asyncio
@pytest.mark.skipif(os.name == "nt", reason="POSIX socket runtime and /bin/echo backend")
async def test_long_wire_path_supports_subscription_prompt_and_cancel(tmp_path):
    comms = wire(tmp_path / ("long-wire-" * 16))
    owner = CommsAgent(comms, agent_bin="/bin/echo", agent_args=[], runtime_enabled=True)
    client = CommsAgent(comms)
    updates = []

    class Client:
        async def session_update(self, session_id, update):
            updates.append(update)

    client.on_connect(Client())
    response = await owner.new_session(str(tmp_path / "project"))
    path = socket_path(comms.root, os.getpid())
    assert len(os.fsencode(path)) < 100
    proxy = RuntimeProxy(client, response.session_id, path)
    try:
        metadata = await proxy.subscribe()
        assert metadata["agentComms"]["ownerPid"] == os.getpid()
        updates.clear()
        result = await proxy.request(
            "prompt", prompt=[{"type": "text", "text": "socket roundtrip"}]
        )
        assert result["stopReason"] == "end_turn"
        await until(
            lambda: any(
                u.get("_meta", {}).get("agentComms", {}).get("turnSettled") for u in updates
            )
        )
        assert any("socket roundtrip" in u.get("content", {}).get("text", "") for u in updates)
        assert await proxy.request("cancel") == {}
        compact_calls = []

        async def compact_context(session_id, instructions):
            compact_calls.append((session_id, instructions))
            return {"ok": True, "status": "compacted"}

        owner.compact_context = compact_context
        assert await proxy.request("compact", instructions="focus") == {
            "ok": True,
            "status": "compacted",
        }
        assert compact_calls == [(response.session_id, "focus")]
        invalid = RuntimeProxy(client, "missing-thread", path)
        with pytest.raises(RuntimeError, match="not registered"):
            await invalid.request("cancel")
    finally:
        await proxy.close()
        await owner.shutdown()
    assert not path.exists()


@pytest.mark.asyncio
@pytest.mark.skipif(os.name == "nt", reason="POSIX socket runtime")
async def test_existing_proxy_follows_renamed_owner_restart_and_resubscribes(tmp_path):
    comms = wire(tmp_path / "wire")
    old_pid, new_pid = 901001, 901002
    comms.register(Thread("worker", frozenset(), str(tmp_path), pid=old_pid))
    client = CommsAgent(comms)
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
                writer.write(
                    (
                        json.dumps(
                            {
                                "ready": {
                                    "agentComms": {"imagePrompts": owner == "new"},
                                    "configOptions": [{"id": "model", "currentValue": owner}],
                                }
                            }
                        )
                        + "\n"
                    ).encode()
                )
                writer.write((json.dumps({"update": {"owner": owner}}) + "\n").encode())
                await writer.drain()
                await reader.read()
            else:
                writer.write((json.dumps({"result": {"owner": owner}}) + "\n").encode())
                await writer.drain()
            writer.close()

        return receive

    old_server = await asyncio.start_unix_server(handler("old"), path=old_path)
    new_server = None
    proxy = RuntimeProxy(client, "worker", old_path)
    try:
        await proxy.subscribe()
        await until(lambda: ("worker", {"owner": "old"}) in updates)
        # Session metadata may be filled in after initial owner attachment.
        comms.registry.register(
            replace(comms.registry.require("worker"), session_file=str(tmp_path / "session.jsonl"))
        )
        assert await proxy.request("cancel") == {"owner": "old"}
        comms.registry.rename("worker", "renamed")
        comms.registry.register(replace(comms.registry.require("renamed"), pid=new_pid))
        old_server.close()
        for writer in connections:
            writer.close()
        await old_server.wait_closed()

        # A request made before the replacement socket starts is still unsent.
        request = asyncio.create_task(proxy.request("cancel"))
        await asyncio.sleep(0.15)
        new_server = await asyncio.start_unix_server(handler("new"), path=new_path)
        assert await asyncio.wait_for(request, 3) == {"owner": "new"}
        await until(lambda: ("worker", {"owner": "new"}) in updates)
        await until(
            lambda: (
                "worker",
                {
                    "sessionUpdate": "config_option_update",
                    "configOptions": [{"id": "model", "currentValue": "new"}],
                },
            )
            in updates
        )
        assert client._proxy_image_support["worker"] is True
        assert ("new", "subscribe", "worker") in calls
        assert ("new", "cancel", "worker") in calls
        assert calls.count(("old", "cancel", "worker")) == 1

        # A later thread with the same former name is a distinct incarnation.
        comms.registry.unregister("renamed")
        comms.registry.begin_delete("renamed")
        comms.registry.remove("renamed")
        comms.register(Thread("worker", frozenset(), str(tmp_path), pid=901003))
        with pytest.raises(RuntimeError, match="identity changed"):
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
async def test_request_only_proxy_never_replays_after_request_was_received(tmp_path):
    comms = wire(tmp_path / "wire")
    old_pid, new_pid = 902001, 902002
    comms.register(Thread("worker", frozenset(), str(tmp_path), pid=old_pid))
    old_path = socket_path(comms.root, old_pid)
    new_path = socket_path(comms.root, new_pid)
    old_path.parent.mkdir(parents=True, exist_ok=True)
    received = []

    async def old_owner(reader, writer):
        request = json.loads(await reader.readline())
        received.append(("old", request["action"]))
        comms.registry.register(replace(comms.registry.require("worker"), pid=new_pid))
        writer.close()  # The action may have happened; its result was lost.

    async def new_owner(reader, writer):
        request = json.loads(await reader.readline())
        received.append(("new", request["action"]))
        writer.write(b'{"result": {"ok": true}}\n')
        await writer.drain()
        writer.close()

    class ToadLike:
        _coordination_root = str(comms.root)

    old_server = await asyncio.start_unix_server(old_owner, path=old_path)
    new_server = await asyncio.start_unix_server(new_owner, path=new_path)
    proxy = RuntimeProxy(ToadLike(), "worker", old_path)
    try:
        with pytest.raises(RuntimeError, match="outcome unknown"):
            await proxy.request("set_goal", text="one")
        assert received == [("old", "set_goal")]
        assert await proxy.request("cancel") == {"ok": True}
        assert received == [("old", "set_goal"), ("new", "cancel")]
    finally:
        await proxy.close()
        old_server.close()
        new_server.close()
        await old_server.wait_closed()
        await new_server.wait_closed()
        old_path.unlink(missing_ok=True)
        new_path.unlink(missing_ok=True)


@pytest.mark.asyncio
@pytest.mark.skipif(os.name == "nt", reason="POSIX socket runtime")
async def test_subscriber_receives_identity_before_transcript_replay(tmp_path):
    comms = wire(tmp_path / "wire")
    owner = CommsAgent(comms, agent_bin="/bin/echo", agent_args=[], runtime_enabled=True)
    client = CommsAgent(comms)
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

    owner._replay_transcript = delayed_replay
    proxy = RuntimeProxy(client, response.session_id, socket_path(comms.root, os.getpid()))
    task = asyncio.create_task(proxy.subscribe())
    try:
        await asyncio.wait_for(entered.wait(), 2)
        await until(
            lambda: any(
                update.get("_meta", {}).get("agentComms", {}).get("wireRoot")
                == str(comms.root.resolve())
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
@pytest.mark.skipif(os.name == "nt", reason="POSIX socket runtime")
async def test_attached_client_receives_owner_model_options(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_COMMS_AGENT_MODELS", "test/one,test/two")
    comms = wire(tmp_path / "wire")
    owner = CommsAgent(
        comms,
        agent_bin="/bin/echo",
        agent_args=["--provider", "test", "--model", "one"],
        runtime_enabled=True,
    )
    client = CommsAgent(comms)
    response = await owner.new_session(str(tmp_path / "project"))
    try:
        attached = await client._attach_owner(
            comms.registry.require(response.session_id), response.session_id
        )
        assert attached.config_options[0].current_value == "test/one"
        assert [option.value for option in attached.config_options[0].options] == [
            "test/one",
            "test/two",
        ]
        assert attached.config_options[1].current_value == "medium"
        assert attached.field_meta["agentComms"]["title"] == "project"
        changed = await client.set_config_option("model", response.session_id, "test/two")
        assert changed.config_options[0].current_value == "test/two"
        assert comms.registry.require("project").model == "test/two"
    finally:
        await client.shutdown()
        await owner.shutdown()


@pytest.mark.asyncio
async def test_new_session_metadata_has_a_display_title_before_first_switch(tmp_path):
    comms = wire(tmp_path / "wire")
    agent = CommsAgent(comms, agent_bin="/bin/echo")
    try:
        response = await agent.new_session(str(tmp_path / "project"))
        assert response.field_meta["agentComms"]["title"] == "project"
    finally:
        await agent.shutdown()


@pytest.mark.asyncio
@pytest.mark.skipif(os.name == "nt", reason="POSIX socket runtime")
async def test_attached_snapshot_client_can_page_earlier_transcript(tmp_path):
    comms = wire(tmp_path / "wire")
    owner = CommsAgent(comms, agent_bin="/bin/echo", runtime_enabled=True)
    client = CommsAgent(comms)
    updates = []

    class Client:
        async def session_update(self, session_id, update):
            updates.append(update)

    client.on_connect(Client())
    await client.initialize(1, {"_meta": {"agentComms": {"transcriptSnapshots": True}}})
    response = await owner.new_session(str(tmp_path / "project"))
    transcript = tmp_path / "session.jsonl"
    transcript.write_text(
        "\n".join(
            json.dumps({"type": "message", "message": {"role": "assistant", "content": str(i)}})
            for i in range(50)
        )
        + "\n"
    )
    comms.attach_session(response.session_id, str(transcript))
    proxy = RuntimeProxy(client, response.session_id, socket_path(comms.root, os.getpid()))
    try:
        await proxy.subscribe()
        snapshots = [
            update.get("_meta", {}).get("agentComms", {})
            for update in updates
            if "transcriptPage" in update.get("_meta", {}).get("agentComms", {})
        ]
        assert len(snapshots) == 1
        assert snapshots[0]["transcriptPage"]["has_older"] is True
        assert all(
            "omitted from this bounded view" not in update.get("content", {}).get("text", "")
            for update in updates
        )
    finally:
        await proxy.close()
        await owner.shutdown()


@pytest.mark.asyncio
@pytest.mark.skipif(os.name == "nt", reason="POSIX socket runtime and /bin/echo backend")
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
        # activity_of returns a synthetic idle state before the child starts.
        # Wait for the idle event emitted after its initial turn instead.
        await until(
            lambda: (activity := comms.activity.all_current().get("child")) is not None
            and activity.state.value == "idle"
        )
        # The owner need not broadcast an unsolicited initial answer: a
        # completed local turn is not proof that any channel was addressed.
        assert all(m.sender != "child" for m in comms.channel_history("#all"))
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
            lambda: all(
                any("incoming" in u.get("_meta", {}).get("agentComms", {}) for u in updates)
                for updates in (first_updates, second_updates)
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
            m for m in comms.full_history() if m.sender == "child" and "second round" in m.body
        ]
        assert len(responses) <= 1
        assert all(m.target == "parent" for m in responses)
        assert all("old broadcast must not be delivered" not in m.body for m in responses)
        assert all(m.sender != "child" for m in comms.channel_history("#all"))
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
        with suppress(ChildProcessError):
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
