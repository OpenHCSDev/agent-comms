"""Single-owner attachment and idle wakeup through real processes and sockets."""

import asyncio
import json
import logging
import os
from contextlib import suppress
from dataclasses import replace
from types import SimpleNamespace

import pytest

import agent_comms.agent_events as ae
from agent_comms.acp_extension import (
    CoordinationChangedUpdate,
    TranscriptSnapshotUpdate,
    TurnChangedUpdate,
    decode_updates,
)
from agent_comms.agent_backend import CompactionOutcome, PlacedCompaction
from agent_comms.child_process import ParentedProcess, ProcessIdentity
from agent_comms.comms import Comms, wire
from agent_comms.field_codec import FieldCodec
from agent_comms.owner_protocol import OwnerAnnouncement, OwnerReady, OwnerResult, OwnerUpdate
from agent_comms.pi_counts import PiCount
from agent_comms.pi_vocabulary import HighThinkingLevel
from agent_comms.runtime import RuntimeProxy, RuntimeServer, present_session, socket_path
from agent_comms.thread_identity import RecordedThreadIncarnation
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
        # The pi project extension reads only the failure text.
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
            assert result["kind"] == "owner_failure" and "invalid" in result["error"]
        finally:
            writer.close()
            await writer.wait_closed()
        assert not comms.registry.require(response.session_id).active_turn.executing
    finally:
        await proxy.close()
        await client.shutdown()
        await owner.shutdown()


@pytest.mark.asyncio
@pytest.mark.skipif(os.name == "nt", reason="POSIX socket runtime and /bin/echo backend")
async def test_long_wire_path_supports_subscription_prompt_and_cancel(tmp_path, monkeypatch, native_backend):
    comms = Comms(tmp_path / ("long-wire-" * 16))
    comms.messaging.initialize_private_initial_protocol()
    monkeypatch.setenv("AGENT_COMMS_ROOT", str(comms.root))
    owner = canonical_agent(comms, agent_bin="pi", agent_args=native_backend.native_arguments(), runtime_enabled=True)
    client = canonical_agent(comms)
    updates = []

    class Client:
        async def session_update(self, session_id, update):
            updates.append(update)

    client.on_connect(Client())
    response = await owner.new_session(str(tmp_path / "project"))

    async def native_events(_bin, _args, task, *_pos, **options):
        native_id = "a" * 32
        with options["send_boundary"](None, native_id, task) as authorized:
            assert authorized
        assert options["native_start"](None, native_id, task)
        yield ae.Chunk(text="socket roundtrip")
        yield ae.StreamSettled()
        yield ae.Done(ok=True, text="socket roundtrip")

    monkeypatch.setattr("agent_comms.backend.stream_agent_events", native_events)
    path = socket_path(comms.root, os.getpid())
    assert path.exists()  # Actual bind and the exchanges below prove OS path claim_scope.
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
                any(isinstance(f, TurnChangedUpdate)
                    and not f.state.active.phase.busy and f.state.finished_turn_id is not None
                    for f in decode_updates(u.get("_meta")))
                for u in updates
            )
        )
        assert any("socket roundtrip" in u.get("content", {}).get("text", "") for u in updates)
        assert await proxy.request("cancel") == {}
        compact_calls = []

        async def compact_context(runner, session_id, instructions):
            compact_calls.append((session_id, instructions))
            return PlacedCompaction("summary", "first-kept", PiCount.from_wire(1200))

        monkeypatch.setattr("agent_comms.manual_compaction_bridge.compact_context", compact_context)
        assert FieldCodec.decode(
            CompactionOutcome, await proxy.request("compact", instructions="focus")
        ) == PlacedCompaction("summary", "first-kept", PiCount.from_wire(1200))
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
    comms.registry.declare(comms.registry.new_thread('worker', frozenset(), str(tmp_path), process_identity=old))
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
        writer.write(OwnerResult(result={"ok": True}).wire())
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
        assert attached.config_options[1].current_value == ""
        assert comms.registry.require(response.session_id).thinking_level is None
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
    comms.threads.attach_session((attachment_owner := comms.registry.require(response.session_id)), str(transcript), process=attachment_owner.process_identity)
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
        comms.registry.new_thread(name='parent', tags=frozenset(), worktree=str(tmp_path), session_file=str(session))
    )
    comms.registry.declare(
        comms.registry.new_thread(name='child', tags=frozenset(), worktree=str(tmp_path), process_identity=ProcessIdentity.capture(os.getpid()))
    )
    with pytest.raises(RelationViolationError, match="already exists"):
        comms.threads.create(ForkSpec.from_task(name="child", parent="parent", project=str(tmp_path), model="test/model", thinking_level=HighThinkingLevel, task="duplicate"))
    assert comms.registry.require("child").pid == os.getpid()


@pytest.mark.asyncio
@pytest.mark.skipif(os.name == "nt", reason="POSIX socket runtime")
async def test_subscriber_leaving_mid_transcript_is_not_a_failed_request(
    tmp_path, monkeypatch, caplog, native_backend,
):
    """A subscriber that leaves ends its subscription quietly; a malformed request still fails."""
    comms = Comms(tmp_path / "wire")
    comms.messaging.initialize_private_initial_protocol()
    monkeypatch.setenv("AGENT_COMMS_ROOT", str(comms.root))
    owner = canonical_agent(
        comms, agent_bin="pi", agent_args=native_backend.native_arguments(), runtime_enabled=True
    )
    client = canonical_agent(comms)

    class Client:
        async def session_update(self, session_id, update):
            pass

    client.on_connect(Client())
    response = await owner.new_session(str(tmp_path / "project"))
    entered, release = asyncio.Event(), asyncio.Event()

    async def held_transcript(*args, **kwargs):
        entered.set()
        await release.wait()

    owner.sessions.transcript.subscribe = held_transcript
    proxy = RuntimeProxy(client, response.session_id, socket_path(comms.root, os.getpid()))
    caplog.set_level(logging.ERROR, logger="agent_comms.runtime")
    task = asyncio.create_task(proxy.subscribe())
    try:
        await asyncio.wait_for(entered.wait(), 2)
        await proxy.close()
        release.set()
        with pytest.raises(RuntimeError, match="disconnected during attachment"):
            await task
        await until(lambda: not owner._runtime.clients.get(response.session_id), timeout=5)
        await asyncio.sleep(0.1)
        assert not [r for r in caplog.records if r.getMessage().startswith("Runtime request failed")]
        reader, writer = await asyncio.open_unix_connection(proxy.path)
        try:
            writer.write(b"not a request\n")
            await writer.drain()
            assert "error" in json.loads(await reader.readline())
        finally:
            writer.close()
            await writer.wait_closed()
        failed = [r for r in caplog.records if r.getMessage().startswith("Runtime request failed")]
        assert len(failed) == 1 and isinstance(failed[0].exc_info[1], ValueError)
    finally:
        release.set()
        task.cancel()
        with suppress(asyncio.CancelledError, RuntimeError):
            await task
        await client.shutdown()
        await owner.shutdown()


@pytest.mark.asyncio
async def test_background_failure_is_reported(tmp_path, caplog):
    """Owner work nobody awaits logs its failure once; cancellation logs nothing."""
    runtime = RuntimeServer(SimpleNamespace(_comms=SimpleNamespace(root=tmp_path)))

    async def failing_owner_work():
        raise ValueError("owner work broke")

    async def idle_owner_work():
        await asyncio.Event().wait()

    caplog.set_level(logging.ERROR, logger="agent_comms.runtime")
    failed = runtime.background(failing_owner_work())
    with pytest.raises(ValueError, match="owner work broke"):
        await failed
    cancelled = runtime.background(idle_owner_work())
    await asyncio.sleep(0)
    cancelled.cancel()
    with pytest.raises(asyncio.CancelledError):
        await cancelled
    await asyncio.sleep(0)
    [record] = caplog.records
    assert "failing_owner_work" in record.getMessage()
    assert record.exc_info[1] is failed.exception()
    assert 'raise ValueError("owner work broke")' in caplog.text
