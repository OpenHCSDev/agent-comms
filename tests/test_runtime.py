"""Single-owner attachment and idle wakeup through real processes and sockets."""
from agent_comms.repo import Checkout
from agent_comms.owner_connection import OwnerReplyLostError
from agent_comms.controller import SessionUpdateSink

import asyncio
import json
import os
from contextlib import ExitStack
from dataclasses import replace

import pytest
from acp.schema import (
    ConfigOptionUpdate, SessionConfigOptionSelect, SessionConfigSelectOption, SessionInfoUpdate,
)

from agent_comms.acp_extension import (
    CoordinationChangedUpdate,
    TranscriptSnapshotUpdate,
    decode_updates,
    encode_updates,
)
from agent_comms.child_process import ProcessIdentity
from agent_comms.errors import RelationViolationError
from agent_comms.field_codec import FieldCodec
from agent_comms.input_disposition import InputDispositions, InputDocument
from agent_comms.input_key import BusInputKey
from agent_comms.bus_read import OwnerReadRuntimeRequest
from agent_comms.order_tools import InputDeliveryRead
from agent_comms.owner_connection import OwnerIdentityChangedError, OwnerReplyConnection, RuntimeRequest
from agent_comms.thread_execution import ExternalThreadExecution
from agent_comms.tool_caller import ToolCaller
from agent_comms.comms import wire
from agent_comms.field_codec import RootId
from agent_comms.owner_protocol import OwnerAnnouncement, OwnerReady, OwnerResult, OwnerUpdate
from agent_comms.runtime import (
    RuntimeConnection, RuntimeProxy, present_session, socket_path,
)
from agent_comms.private_path import PrivateSocketRole
from agent_comms.thread_identity import RecordedThreadIncarnation
from agent_comms.session_file import RegistrySessionLocation
from agent_comms.native_arguments import GivenBinary
from delivery_owner_fixture import canonical_agent
from owner_socket_fixture import declare_owner, silent_socket
from stored_inputs import stored_inputs
from agent_comms.threads import Thread


async def test_dispatched_request_without_a_reply_remains_unknown_and_is_not_replayed(comms):
    process = ProcessIdentity.capture(os.getpid())
    declare_owner(comms, "reply-loss", process)
    async with silent_socket(comms, process) as received:
        connection = RuntimeConnection(comms, "reply-loss", socket_path(comms.root, process.pid))
        try:
            with pytest.raises(OwnerReplyLostError) as lost:
                await connection.request("cancel")
            assert isinstance(lost.value, RuntimeError)
            assert isinstance(lost.value, ConnectionError)
            assert [request["action"] for request in received] == ["cancel"]
        finally:
            await connection.close()


def recorded_input(comms) -> InputDocument:
    """One durable input of the read's subject; a refused or lost read leaves it unchanged."""
    ledger = InputDispositions(comms.root)
    ledger.record(BusInputKey(5), seq=5, owner="held", claim_scope=1, target="held", text="Kept")
    return stored_inputs(ledger.root)


async def mcp_input_delivery(comms, include_history: bool = True):
    async with asyncio.timeout(30):
        return await asyncio.to_thread(
            InputDeliveryRead.invoke, comms, {"thread": "held", "include_history": include_history},
            caller=ToolCaller.from_actor(None),
        )


@pytest.mark.parametrize("include_history", [False, True])
async def test_an_input_delivery_read_without_a_reply_is_lost_after_one_dispatch(comms, include_history):
    """The owner received exactly the declared operation once, then hung up: the caller
    gets the original reply loss, and no durable input moved. The peer runs no domain body."""
    process = ProcessIdentity.capture(os.getpid())
    declare_owner(comms, "held", process)
    before = recorded_input(comms)
    async with silent_socket(comms, process) as received:
        with pytest.raises(OwnerReplyLostError):
            await mcp_input_delivery(comms, include_history)
    assert [FieldCodec.decode(RuntimeRequest, request) for request in received] == [
        OwnerReadRuntimeRequest(
            thread="held", operation=InputDeliveryRead(thread="held", include_history=include_history),
        ),
    ]
    assert stored_inputs(comms.root) == before


async def test_a_held_owner_read_refuses_a_replaced_root_before_dispatch(comms):
    process = ProcessIdentity.capture(os.getpid())
    owner = declare_owner(comms, "held", process)
    reading = OwnerReadRuntimeRequest(thread="held", operation=InputDeliveryRead(thread="held"))
    async with silent_socket(comms, process) as received:
        connection = OwnerReplyConnection.held(comms, owner.incarnation, reading)
        moved = comms.root.with_name(comms.root.name + "-replaced")
        comms.root.rename(moved)
        comms.root.mkdir()
        try:
            with pytest.raises(OwnerIdentityChangedError):
                async with asyncio.timeout(30):
                    await reading.read(connection)
        finally:
            comms.root.rmdir()
            moved.rename(comms.root)
            await connection.close()
    assert received == []


async def test_a_same_name_recreation_refuses_the_held_read_and_the_old_witness(comms):
    process = ProcessIdentity.capture(os.getpid())
    old = declare_owner(comms, "held", process)
    reading = OwnerReadRuntimeRequest(thread="held", operation=InputDeliveryRead(thread="held"))
    async with silent_socket(comms, process) as received:
        connection = OwnerReplyConnection.held(comms, old.incarnation, reading)
        try:
            comms.registry.unregister("held")
            comms.registry.begin_delete("held")
            with comms.registry.store.editing() as edit:
                edit.document.remove_originals((old,))
                edit.commit()
            new = declare_owner(comms, "held", process)
            assert new.name == old.name and new.incarnation != old.incarnation
            assert comms.registry.require("held").incarnation == new.incarnation
            with pytest.raises(OwnerIdentityChangedError):
                async with asyncio.timeout(30):
                    await reading.read(connection)
            with pytest.raises(OwnerIdentityChangedError):
                OwnerReplyConnection.held(comms, old.incarnation, reading)
        finally:
            await connection.close()
    assert received == []


async def test_a_held_owner_read_refuses_after_its_process_dies(comms, runtime_processes):
    child = runtime_processes[0]
    owner = declare_owner(comms, "held", child.identity)
    reading = OwnerReadRuntimeRequest(thread="held", operation=InputDeliveryRead(thread="held"))
    before = recorded_input(comms)
    async with silent_socket(comms, child.identity) as received:
        connection = OwnerReplyConnection.held(comms, owner.incarnation, reading)
        try:
            child.stop_sync()
            assert not child.identity.alive()
            with pytest.raises(ConnectionError):
                async with asyncio.timeout(30):
                    await reading.read(connection)
        finally:
            await connection.close()
    assert received == []
    assert stored_inputs(comms.root) == before


async def test_an_external_owner_refuses_the_input_delivery_read_before_any_socket_wait(
    comms, runtime_processes,
):
    declare_owner(comms, "held", runtime_processes[0].identity, ExternalThreadExecution)
    before = recorded_input(comms)
    with pytest.raises(RelationViolationError):
        await mcp_input_delivery(comms)
    assert stored_inputs(comms.root) == before


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
                session_id="canonical",
                wire_root_id=RootId("a" * 32),
                claimed_identity=ClaimedIdentity(RecordedThreadIncarnation("canonical", 1000.0), 3),
                owner=ProcessIdentity(1234, 1),
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
async def test_subscription_replay_uses_requested_alias(
    tmp_path, request_original, private_comms, native_runtime_package
):
    from agent_comms.coordinator import Coordination
    from agent_comms.launch_root import LaunchRoot

    comms = private_comms
    LaunchRoot.from_path(comms.root).install()
    owner = canonical_agent(comms, runtime_enabled=True, auto_wake=False)
    client = canonical_agent(comms, auto_wake=False)
    updates = []

    class Client:
        async def publish_update(self, session_id, update):
            updates.append((session_id, update.model_dump(mode="json", by_alias=True, exclude_none=True)))
            return True

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
            store.participants.register(thread.incarnation.lookup, committed=True)
        comms.threads.attach_session(thread, str(journal), process=thread.process_identity)
        comms.threads.rename_managed_thread(original, "renamed", owner=ProcessIdentity.capture(os.getpid()))
        requested = original if request_original else "renamed"
        proxy = RuntimeProxy(client, requested, socket_path(comms.root, os.getpid()))
        await proxy.subscribe()
        snapshots = [(session, fact) for session, update in updates
                     for fact in decode_updates(update.get("_meta"))
                     if isinstance(fact, TranscriptSnapshotUpdate)]
        ordered = [fact for _, update in updates for fact in decode_updates(update.get('_meta'))]
        identity_position = next(index for index, fact in enumerate(ordered)
                                 if isinstance(fact, CoordinationChangedUpdate))
        replay_position = next(index for index, fact in enumerate(ordered)
                               if isinstance(fact, TranscriptSnapshotUpdate))
        assert identity_position < replay_position
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
    old, new = (child.identity for child in runtime_processes)
    old_pid, new_pid = old.pid, new.pid
    comms.registry.declare(Thread('worker', frozenset(), Checkout(tmp_path), process_identity=old))
    client = canonical_agent(comms)
    updates = []

    class Client(SessionUpdateSink):
        async def publish_update(self, *, session_id, update):
            updates.append((session_id, update.model_dump(mode="json", by_alias=True, exclude_none=True)))
            return True

    client.on_connect(Client())
    old_path = socket_path(comms.root, old_pid)
    new_path = socket_path(comms.root, new_pid)
    old_path.parent.mkdir(parents=True, exist_ok=True)
    connections = []
    calls = []

    def model_menu(owner):
        return ConfigOptionUpdate(
            session_update="config_option_update",
            config_options=[SessionConfigOptionSelect(
                type="select", id="model", name="Model", current_value=owner,
                options=[SessionConfigSelectOption(value=owner, name=owner)],
            )],
        )

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
                        controller_token="a" * 64,
                    ).wire()
                )
                writer.write(OwnerUpdate.of(model_menu(owner)).wire())
                writer.write(OwnerUpdate.of(SessionInfoUpdate(
                    session_update="session_info_update", title=owner,
                    field_meta={"owner": owner},
                )).wire())
                await writer.drain()
                await reader.read()
            else:
                writer.write(OwnerResult(result={"owner": owner}).wire())
                await writer.drain()
            writer.close()

        return receive

    addresses = ExitStack()
    old_address = addresses.enter_context(PrivateSocketRole.address(old_path))
    new_address = addresses.enter_context(PrivateSocketRole.address(new_path))
    old_server = await asyncio.start_unix_server(handler("old"), path=old_address)
    new_server = None
    proxy = RuntimeProxy(client, "worker", old_path)
    try:
        await proxy.subscribe()
        await until(
            lambda: any(
                session == "worker" and data.get("_meta", {}).get("owner") == "old" for session, data in updates
            )
        )
        # Session metadata may be filled in after initial owner attachment.
        comms.registry.register(
            replace(comms.registry.require("worker"), session_file=RegistrySessionLocation.from_path(str(tmp_path / "session.jsonl"), comms.root))
        )
        assert await proxy.request("cancel") == {"owner": "old"}
        comms.threads.rename_managed_thread("worker", "renamed", owner=old)
        comms.registry.register(replace(comms.registry.require("renamed"), process_identity=new))
        old_server.close()
        for writer in connections:
            writer.close()
        await old_server.wait_closed()

        # A request made before the replacement socket starts is still unsent.
        request = asyncio.create_task(proxy.request("cancel"))
        await asyncio.sleep(0.15)
        new_server = await asyncio.start_unix_server(handler("new"), path=new_address)
        assert await asyncio.wait_for(request, 3) == {"owner": "new"}
        await until(
            lambda: any(
                session == "worker" and data.get("_meta", {}).get("owner") == "new" for session, data in updates
            )
        )
        await until(
            lambda: (
                (
                    "worker",
                    {
                        **model_menu("new").model_dump(mode="json", by_alias=True, exclude_none=True),
                        "_meta": encode_updates(),
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
            Thread('worker', frozenset(), Checkout(tmp_path), process_identity=ProcessIdentity(901003, 1))
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
        addresses.close()






@pytest.mark.asyncio
async def test_new_session_metadata_has_a_display_title_before_first_switch(
    tmp_path, native_runtime_package
):
    comms = wire(tmp_path / "wire")
    agent = canonical_agent(comms, agent_bin=GivenBinary("/bin/echo"))
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
