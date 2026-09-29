"""Single-owner attachment and idle wakeup through real processes and sockets."""

import asyncio
import json
import os
from dataclasses import replace

import pytest

from agent_comms import agent_events as ae
from agent_comms.acp_extension import (
    CoordinationChangedUpdate,
    TranscriptSnapshotUpdate,
    TurnSettledUpdate,
    decode_updates,
)
from agent_comms.child_process import ParentedProcess, ProcessIdentity
from agent_comms.comms import wire
from agent_comms.compaction_result import CommittedCompactionResult, CompactionResult
from agent_comms.field_codec import FieldCodec
from agent_comms.runtime import RuntimeProxy, present_session, socket_path
from agent_comms.thread_management import ForkSpec
from agent_comms.threads import Thread
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
        CursorScope,
        EmptyCursorObservation,
        decode_updates,
        encode_updates,
    )
    from agent_comms.thread_identity import OwnerIdentity, ThreadIncarnation

    fact = CursorAdvancedUpdate(
        CursorEnvelope(
            CursorScope(
                "canonical",
                "a" * 32,
                OwnerIdentity(ThreadIncarnation("canonical", 1000.0), 3),
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


async def until(predicate, timeout=10):
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(0.05)


@pytest.mark.asyncio
@pytest.mark.skipif(os.name == "nt", reason="POSIX socket runtime")
async def test_owner_prompt_rejection_preserves_reason_and_rpc_code(tmp_path):
    from acp.exceptions import RequestError

    comms = wire(tmp_path / "wire")
    owner = canonical_agent(comms, agent_bin="/bin/echo", agent_args=[], runtime_enabled=True)
    client = canonical_agent(comms)
    response = await owner.new_session(str(tmp_path / "project"))
    proxy = RuntimeProxy(client, response.session_id, socket_path(comms.root, os.getpid()))
    request = {
        "prompt": [{"type": "text", "text": "Do not launch"}],
        "meta": {"agentComms": {"request": {"kind": "invalid"}}},
    }
    try:
        with pytest.raises(RequestError) as caught:
            await proxy.request("prompt", **request)
        assert caught.value.code == -32602
        assert "invalid" in caught.value.data["reason"]
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
            assert "invalid" in result["error"]
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
async def test_long_wire_path_supports_subscription_prompt_and_cancel(tmp_path, monkeypatch):
    comms = wire(tmp_path / ("long-wire-" * 16))
    owner = canonical_agent(comms, agent_bin="/bin/echo", agent_args=[], runtime_enabled=True)
    client = canonical_agent(comms)
    updates = []

    class Client:
        async def session_update(self, session_id, update):
            updates.append(update)

    client.on_connect(Client())
    response = await owner.new_session(str(tmp_path / "project"))

    async def native_events(_bin, _args, task, *_pos, **options):
        native_id = "a" * 32
        with options["send_boundary"](None, native_id, task) as admitted:
            assert admitted
        assert options["native_start"](None, native_id, task)
        yield ae.Chunk(text="socket roundtrip")
        yield ae.StreamSettled()
        yield ae.Done(ok=True, text="socket roundtrip")

    monkeypatch.setattr("agent_comms.backend.stream_agent_events", native_events)
    path = socket_path(comms.root, os.getpid())
    assert path.exists()  # Actual bind and the exchanges below prove OS path admission.
    proxy = RuntimeProxy(client, response.session_id, path)
    try:
        metadata = await proxy.subscribe()
        assert (
            next(
                f for f in decode_updates(metadata) if isinstance(f, CoordinationChangedUpdate)
            ).owner_pid
            == os.getpid()
        )
        updates.clear()
        result = await proxy.request(
            "prompt", prompt=[{"type": "text", "text": "socket roundtrip"}]
        )
        assert result["stopReason"] == "end_turn"
        await until(
            lambda: any(
                any(isinstance(f, TurnSettledUpdate) for f in decode_updates(u.get("_meta")))
                for u in updates
            )
        )
        assert any("socket roundtrip" in u.get("content", {}).get("text", "") for u in updates)
        assert await proxy.request("cancel") == {}
        compact_calls = []

        async def compact_context(runner, session_id, instructions):
            compact_calls.append((session_id, instructions))
            return CommittedCompactionResult("summary", "commit")

        monkeypatch.setattr("agent_comms.manual_compaction_bridge.compact_context", compact_context)
        assert FieldCodec.decode(
            CompactionResult, await proxy.request("compact", instructions="focus")
        ) == CommittedCompactionResult("summary", "commit")
        assert compact_calls == [(response.session_id, "focus")]
        with pytest.raises(ValueError, match="not registered"):
            RuntimeProxy(client, "missing-thread", path)
    finally:
        await proxy.close()
        await owner.shutdown()
    assert not path.exists()


@pytest.mark.asyncio
@pytest.mark.skipif(os.name == "nt", reason="POSIX socket runtime")
async def test_existing_proxy_follows_renamed_owner_restart_and_resubscribes(
    tmp_path, runtime_processes
):
    comms = wire(tmp_path / "wire")
    old, new = runtime_processes
    old_pid, new_pid = old.pid, new.pid
    comms.registry.declare(Thread("worker", frozenset(), str(tmp_path), process_identity=old))
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
                writer.write(
                    (
                        json.dumps(
                            {
                                "controllerToken": "a" * 64,
                                "ready": {
                                    "agentComms": {"updates": []},
                                    "configOptions": [{"id": "model", "currentValue": owner}],
                                },
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
            Thread(
                "worker", frozenset(), str(tmp_path), process_identity=ProcessIdentity(901003, 1)
            )
        )
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
async def test_request_only_proxy_never_replays_after_request_was_received(
    tmp_path, runtime_processes
):
    comms = wire(tmp_path / "wire")
    old, new = runtime_processes
    old_pid, new_pid = old.pid, new.pid
    comms.registry.declare(Thread("worker", frozenset(), str(tmp_path), process_identity=old))
    old_path = socket_path(comms.root, old_pid)
    new_path = socket_path(comms.root, new_pid)
    old_path.parent.mkdir(parents=True, exist_ok=True)
    received = []

    async def old_owner(reader, writer):
        request = json.loads(await reader.readline())
        received.append(("old", request["action"]))
        comms.registry.register(replace(comms.registry.require("worker"), process_identity=new))
        writer.close()  # The action may have happened; its result was lost.

    async def new_owner(reader, writer):
        request = json.loads(await reader.readline())
        received.append(("new", request["action"]))
        writer.write(b'{"result": {"ok": true}}\n')
        await writer.drain()
        writer.close()

    from types import SimpleNamespace

    context = SimpleNamespace(_comms=comms)

    old_server = await asyncio.start_unix_server(old_owner, path=old_path)
    new_server = await asyncio.start_unix_server(new_owner, path=new_path)
    proxy = RuntimeProxy(context, "worker", old_path)
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
@pytest.mark.skipif(os.name == "nt", reason="POSIX socket runtime")
async def test_attached_client_receives_owner_model_options(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_COMMS_AGENT_MODELS", "test/one,test/two")
    comms = wire(tmp_path / "wire")
    owner = canonical_agent(
        comms,
        agent_bin="/bin/echo",
        agent_args=["--provider", "test", "--model", "one"],
        runtime_enabled=True,
    )
    client = canonical_agent(comms)
    response = await owner.new_session(str(tmp_path / "project"))
    try:
        attached = await client.sessions.attach_owner(
            comms.registry.require(response.session_id), response.session_id
        )
        assert attached.config_options[0].current_value == "test/one"
        assert [option.value for option in attached.config_options[0].options] == [
            "test/one",
            "test/two",
        ]
        assert attached.config_options[1].current_value == "medium"
        assert (
            next(
                f
                for f in decode_updates(attached.field_meta)
                if isinstance(f, CoordinationChangedUpdate)
            ).title
            == "project"
        )
        changed = await client.set_config_option("model", response.session_id, "test/two")
        assert changed.config_options[0].current_value == "test/two"
        assert comms.registry.require("project").model == "test/two"
    finally:
        await client.shutdown()
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


@pytest.mark.asyncio
@pytest.mark.skipif(os.name == "nt", reason="POSIX socket runtime")
async def test_attached_snapshot_client_can_page_earlier_transcript(tmp_path):
    comms = wire(tmp_path / "wire")
    owner = canonical_agent(comms, agent_bin="/bin/echo", runtime_enabled=True)
    client = canonical_agent(comms)
    updates = []

    class Client:
        async def session_update(self, session_id, update):
            updates.append(update)

    client.on_connect(Client())
    await client.initialize(1, {})
    response = await owner.new_session(str(tmp_path / "project"))
    transcript = tmp_path / "session.jsonl"
    transcript.write_text(
        "\n".join(
            json.dumps({"type": "message", "message": {"role": "assistant", "content": str(i)}})
            for i in range(50)
        )
        + "\n"
    )
    comms.threads.attach_session(response.session_id, str(transcript))
    proxy = RuntimeProxy(client, response.session_id, socket_path(comms.root, os.getpid()))
    try:
        await proxy.subscribe()
        snapshots = [
            f
            for update in updates
            for f in decode_updates(update.get("_meta"))
            if isinstance(f, TranscriptSnapshotUpdate)
        ]
        assert len(snapshots) == 1
        assert snapshots[0].page.has_older is True
        assert all(
            "omitted from this bounded view" not in update.get("content", {}).get("text", "")
            for update in updates
        )
    finally:
        await proxy.close()
        await owner.shutdown()


def test_fork_rejects_duplicate_instead_of_overwriting_owner(tmp_path):
    from agent_comms.errors import RelationViolationError

    comms = wire(tmp_path)
    session = tmp_path / "parent.jsonl"
    session.write_text("")
    comms.registry.declare(
        Thread(name="parent", tags=frozenset(), worktree=str(tmp_path), session_file=str(session))
    )
    comms.registry.declare(
        Thread(
            name="child",
            tags=frozenset(),
            worktree=str(tmp_path),
            process_identity=ProcessIdentity.capture(os.getpid()),
        )
    )
    with pytest.raises(RelationViolationError, match="already exists"):
        comms.threads.fork(ForkSpec(name="child", parent="parent", task="duplicate"))
    assert comms.registry.require("child").pid == os.getpid()
