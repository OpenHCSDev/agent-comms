"""Explicit ACP session owner consumes ordinary private N/K without legacy ACK.

Only model responses are faked; this does not prove provider delivery or
activate private processing for existing public sessions.
"""

from __future__ import annotations

import asyncio
import json
import os

import pytest

from agent_comms import cohort_foreground, coordinated_runtime
from agent_comms.acp import CommsAgent
from agent_comms.acp_extension import (
    CursorAdvancedUpdate,
    VerifiedCursorObservation,
    decode_updates,
)
from agent_comms.bus_publication import stable_thread_lookup
from agent_comms.child_process import ProcessIdentity
from agent_comms.cohort_foreground import _accept_visible_deliveries
from agent_comms.comms import Comms
from agent_comms.coordination_errors import PublicationActivationBlocked, StaleFence
from agent_comms.coordinator import Coordination
from agent_comms.errors import RelationViolationError
from agent_comms.goal_actions import SetGoalAction
from agent_comms.message_bus import MessageBus
from agent_comms.native_pi import NativePiUnavailable
from agent_comms.native_runtime_input import NativeRuntimeInput
from agent_comms.participant_store import ParticipantStore
from agent_comms.store_files import _store_lock
from agent_comms.threads import Thread
from agent_comms.tools import invoke_tool
from agent_comms.tracked_turn import TrackedTurnSession
from test_coordinated_runtime import _fake_model
from test_coordinated_runtime import tmp_path as private_root_fixture
from test_native_prompt_binding import _fake_model as separate_session_fake
from test_backend_native_lifecycle import native_backend as native_backend
from test_goal_standby_liveness import retained_native_acp_owner as retained_native_acp_owner

tmp_path = private_root_fixture


def _session(tmp_path, *, package=True):
    root = tmp_path / "wire"
    comms = Comms(root)
    comms.registry.declare(
        Thread(
            "sender",
            frozenset(),
            str(tmp_path),
            process_identity=ProcessIdentity.capture(os.getpid()),
        )
    )
    owner = Thread(
        "beta",
        frozenset({"team"}),
        str(tmp_path),
        process_identity=ProcessIdentity.capture(os.getpid()),
        model="openai-codex/gpt-6-sol",
    )
    comms.registry.declare(owner)
    root_id = comms.messaging.initialize_private_initial_protocol()
    with Coordination(str(root / "coordination.sqlite3")) as store:
        store.participants.register(
            stable_thread_lookup(owner.created_at), "beta", "beta", committed=True
        )
    agent = CommsAgent(
        comms,
        runtime_enabled=True,
        private_nk_native_package=tmp_path if package else None,
        private_nk_wire_root_id=root_id if package else None,
    )
    agent.sessions.bindings["beta"] = "beta"
    agent.sessions.titles["beta"] = "beta"
    agent.sessions.worktrees["beta"] = str(tmp_path)
    return comms, agent, root_id


def cursor_fact(metadata):
    return next(
        (update for update in decode_updates(metadata) if isinstance(update, CursorAdvancedUpdate)),
        None,
    )


def cursor_envelope(metadata):
    fact = cursor_fact(metadata)
    assert fact is not None
    return fact.envelope


async def test_acp_new_session_owner_consumes_private_selected_source(tmp_path, monkeypatch):
    root = tmp_path / "wire"
    project = tmp_path / "proj"
    project.mkdir()
    comms = Comms(root)
    comms.registry.declare(
        Thread(
            "sender",
            frozenset(),
            str(tmp_path),
            process_identity=ProcessIdentity.capture(os.getpid()),
        )
    )
    root_id = comms.messaging.initialize_private_initial_protocol()
    agent = CommsAgent(
        comms,
        runtime_enabled=True,
        private_nk_native_package=tmp_path,
        private_nk_wire_root_id=root_id,
    )

    async def noop(*args, **kwargs):
        return None

    async def no_options(*args, **kwargs):
        return []

    monkeypatch.setattr(agent._runtime, "start", noop)
    monkeypatch.setattr(agent.inputs, "ensure_live_drain", lambda _: None)
    monkeypatch.setattr(agent.sessions.config, "options", no_options)
    monkeypatch.setattr(cohort_foreground, "_trusted_package", lambda _: None)
    monkeypatch.setattr(coordinated_runtime, "_trusted_package", lambda _: None)
    fake, calls = _fake_model(decision="FULL")
    monkeypatch.setattr(TrackedTurnSession, "execute", fake)
    session = await agent.new_session(cwd=str(project), mcp_servers=[])
    owner = comms.registry.require(session.session_id)
    with Coordination(str(root / "coordination.sqlite3")) as store:
        store.participants.register(
            stable_thread_lookup(owner.created_at), owner.name, owner.name, committed=True
        )
    invoke_tool(
        comms,
        "comms_send",
        {"from": "sender", "to": owner.name, "body": "Compute 17+25"},
    )
    assert await agent.inputs.drain_inbox(session.session_id) == 1
    assert len(calls) == 1


@pytest.mark.parametrize("managed", [False, True])
async def test_private_owner_rename_migrates_generation_before_canonical_selected_send(
    tmp_path, monkeypatch, managed
):
    comms, agent, _ = _session(tmp_path)
    lookup = stable_thread_lookup(comms.registry.require("beta").created_at)
    with Coordination(str(comms.root / "coordination.sqlite3")) as store:
        before = store.participants.get(lookup)
    assert before.owner_thread == "beta" and before.participant_generation == 1
    renamed = (
        comms.threads.rename_managed_thread("beta", "gamma", owner_pid=os.getpid())
        if managed
        else comms.threads._rename_thread("beta", "gamma")
    )
    assert renamed.previous == "beta" and renamed.current == "gamma"
    with Coordination(str(comms.root / "coordination.sqlite3")) as store:
        after = store.participants.get(lookup)
    assert after.owner_thread == "gamma" and after.participant_generation == 2
    assert comms.registry.require("beta").name == "gamma"
    assert not (comms.root / ".private-owner-rename.pending").exists()

    monkeypatch.setattr(cohort_foreground, "_trusted_package", lambda _: None)
    monkeypatch.setattr(coordinated_runtime, "_trusted_package", lambda _: None)
    fake, calls = _fake_model(decision="FULL")
    monkeypatch.setattr(TrackedTurnSession, "execute", fake)
    invoke_tool(comms, "comms_send", {"from": "sender", "to": "beta", "body": "new owner"})
    assert await agent.inputs.drain_inbox("beta") == 1
    assert len(calls) == 1
    assert agent.sessions.bindings["beta"] == "gamma"
    assert await agent.inputs.drain_inbox("beta") == 0  # no second model send


@pytest.mark.parametrize("seal_old", [False, True])
async def test_private_rename_does_not_replay_unserved_old_name_selected_source(
    tmp_path, monkeypatch, seal_old
):
    comms, agent, root_id = _session(tmp_path)
    lookup = stable_thread_lookup(comms.registry.require("beta").created_at)
    invoke_tool(comms, "comms_send", {"from": "sender", "to": "beta", "body": "old-before-rename"})
    if seal_old:
        bus = MessageBus(comms.root / "bus.jsonl", comms.registry, private_response_writes=True)
        with Coordination(str(comms.root / "coordination.sqlite3")) as store:
            await _accept_visible_deliveries(bus, root_id, store.session.path, lookup, 0, owner_name="beta")
    comms.threads._rename_thread("beta", "gamma")
    invoke_tool(comms, "comms_send", {"from": "sender", "to": "gamma", "body": "new-after-rename"})
    monkeypatch.setattr(cohort_foreground, "_trusted_package", lambda _: None)
    monkeypatch.setattr(coordinated_runtime, "_trusted_package", lambda _: None)
    fake, calls = _fake_model(decision="FULL")
    monkeypatch.setattr(TrackedTurnSession, "execute", fake)
    assert await agent.inputs.drain_inbox("beta") == 1
    assert len(calls) == 1
    assert "new-after-rename" in calls[0][1]
    assert "old-before-rename" not in calls[0][1]
    assert await agent.inputs.drain_inbox("beta") == 0


async def test_private_rename_old_name_backlog_does_not_exhaust_new_recipient_scan(
    tmp_path, monkeypatch
):
    comms, agent, _ = _session(tmp_path)
    for index in range(101):
        comms.messaging.send_message("sender", "beta", f"old {index}")
    comms.threads._rename_thread("beta", "gamma")
    invoke_tool(comms, "comms_send", {"from": "sender", "to": "gamma", "body": "new canonical"})
    monkeypatch.setattr(cohort_foreground, "_trusted_package", lambda _: None)
    monkeypatch.setattr(coordinated_runtime, "_trusted_package", lambda _: None)
    fake, calls = _fake_model(decision="FULL")
    monkeypatch.setattr(TrackedTurnSession, "execute", fake)
    assert await agent.inputs.drain_inbox("beta") == 1
    assert len(calls) == 1 and "new canonical" in calls[0][1]
    assert await agent.inputs.drain_inbox("beta") == 0


def test_private_rename_refuses_mismatched_sql_owner_before_registry_mutation(tmp_path):
    comms, _, _ = _session(tmp_path)
    lookup = stable_thread_lookup(comms.registry.require("beta").created_at)
    with Coordination(str(comms.root / "coordination.sqlite3")) as store:
        store.participants.advance_generation(lookup, "unexpected", expected_generation=1)
    with pytest.raises(RelationViolationError, match="Private coordinator owner differs"):
        comms.threads._rename_thread("beta", "gamma")
    assert comms.registry.require("beta").name == "beta"
    assert "gamma" not in comms.registry
    assert comms.bus.log.latest_sequence() == 0


@pytest.mark.asyncio
async def test_private_rename_compensates_registry_failure_with_new_old_owner_generation(
    tmp_path, monkeypatch
):
    comms, _, root_id = _session(tmp_path)
    lookup = stable_thread_lookup(comms.registry.require("beta").created_at)
    original = comms.registry.rename

    def fail_before_registry_write(name, new_name):
        raise OSError("synthetic registry persistence failure")

    monkeypatch.setattr(comms.registry, "rename", fail_before_registry_write)
    with pytest.raises(RelationViolationError, match="Private owner rename is uncertain"):
        comms.threads._rename_thread("beta", "gamma")
    monkeypatch.setattr(comms.registry, "rename", original)
    assert comms.registry.require("beta").name == "beta"
    with Coordination(str(comms.root / "coordination.sqlite3")) as store:
        person = store.participants.get(lookup)
    assert person.owner_thread == "beta" and person.participant_generation == 3
    assert (comms.root / ".private-owner-rename.pending").is_file()
    with pytest.raises(RelationViolationError, match="Private owner rename is pending"):
        comms.messaging.send_message("sender", "beta", "not published after uncertain rename")
    bus = MessageBus(comms.root / "bus.jsonl", comms.registry, private_response_writes=True)
    with (
        Coordination(str(comms.root / "coordination.sqlite3")) as store,
        pytest.raises(RelationViolationError, match="Private owner rename is pending"),
    ):
        await _accept_visible_deliveries(bus, root_id, store.session.path, lookup, 0, owner_name="beta")
    assert comms.bus.log.latest_sequence() == 0


async def test_private_rename_intent_fences_inflight_reserved_native_send(tmp_path, monkeypatch):
    comms, agent, _ = _session(tmp_path)
    monkeypatch.setattr(cohort_foreground, "_trusted_package", lambda _: None)
    monkeypatch.setattr(coordinated_runtime, "_trusted_package", lambda _: None)
    fake, calls = _fake_model(decision="FULL")
    entered, release = asyncio.Event(), asyncio.Event()

    async def held_before_raw_send(*args, **kwargs):
        entered.set()
        await release.wait()
        return await fake(*args, **kwargs)

    monkeypatch.setattr(TrackedTurnSession, "execute", held_before_raw_send)
    invoke_tool(
        comms, "comms_send", {"from": "sender", "to": "beta", "body": "selected before rename"}
    )
    running = asyncio.create_task(agent.inputs.drain_inbox("beta"))
    await asyncio.wait_for(entered.wait(), timeout=5)
    with Coordination(str(comms.root / "coordination.sqlite3")) as store:
        reserved_count = len(NativeRuntimeInput.select(store.session._connection))
        assert reserved_count == 1

    def fail_before_sql(*args, **kwargs):
        raise OSError("synthetic SQL rename CAS outage")

    monkeypatch.setattr(ParticipantStore, "advance_generation", fail_before_sql)
    with pytest.raises(OSError, match="synthetic SQL rename CAS outage"):
        comms.threads._rename_thread("beta", "gamma")
    assert (comms.root / ".private-owner-rename.pending").is_file()
    assert comms.registry.require("beta").name == "beta"
    release.set()
    with pytest.raises(RelationViolationError, match="Private owner rename is pending"):
        await asyncio.wait_for(running, timeout=8)
    assert calls == []  # No raw native send; reserved outcome remains UNKNOWN, never retried.
    with Coordination(str(comms.root / "coordination.sqlite3")) as store:
        assert (
            len(
                NativeRuntimeInput.select(
                    store.session._connection, where="sent_owner_admission_generation IS NOT NULL"
                )
            )
            == 0
        )


async def test_acp_session_selected_native_pipeline_never_uses_legacy_ack(
    retained_native_acp_owner,
):
    native, comms, agent, sid = retained_native_acp_owner
    # The shared standby fixture disables autonomous wake. This explicit private
    # delivery journey enables the real owner policy, with its observer stopped.
    agent.inputs.auto_wake = True
    comms.registry.declare(
        Thread(
            "sender",
            frozenset(),
            str(native.project),
            process_identity=ProcessIdentity.capture(os.getpid()),
        )
    )
    chunks = native.provider.response_chunks

    def controlled_response():
        import json

        native.provider.text = (
            '{"decision":"FULL"}'
            if "bounded triage" in json.dumps(native.provider.requests[-1])
            else "42"
        )
        yield from chunks()

    native.provider.response_chunks = controlled_response
    with comms.bus.log.locked():
        root_id = comms.bus.log.read_metadata_unlocked().root_id
    sent = invoke_tool(comms, "comms_send", {"from": "sender", "to": sid, "body": "Compute 17+25"})
    original = comms.bus.log.message_by_id(sent["id"])
    before = cursor_envelope((await agent.sessions.metadata(sid)))
    assert before.status == "none"
    assert before.revision >= 1
    assert before.scope.admission.incarnation.name == sid
    assert before.scope.wire_root_id == root_id
    updates = []

    class Client:
        async def session_update(self, *, session_id, update):
            assert session_id == sid
            updates.append(update)

    agent.on_connect(Client())
    async with asyncio.timeout(30):
        assert await agent.inputs.drain_inbox(sid) == 1
    current = cursor_envelope((await agent.sessions.metadata(sid)))
    assert current.status == "proven"
    (reply,) = comms.bus.inbox("sender")
    assert reply.sender == sid and reply.body == "42"
    assert current.observation.cursor.injected_seq == original.seq
    assert current.observation.cursor.covered_seq == reply.seq
    assert reply.seq > original.seq
    announced_update = next(
        update for update in reversed(updates) if cursor_fact(update.field_meta)
    )
    announced = cursor_envelope(announced_update.field_meta)
    assert announced.scope == current.scope
    assert before.revision < announced.revision < current.revision
    assert announced.observation.cursor.input_id == current.observation.cursor.input_id
    assert cursor_fact(announced_update.field_meta).selected_status == "proven"
    calls = native.provider.posts
    assert calls > 0
    with Coordination(str(comms.root / "coordination.sqlite3")) as store:
        assert (
            len(
                NativeRuntimeInput.select(store.session._connection, where="session_id IS NOT NULL")
            )
            == 1
        )
        assert (
            store.session._connection.execute(
                "SELECT COUNT(*) FROM claim_batch_receipts WHERE wire_root_id=? AND wire_seq=?",
                (root_id, original.seq),
            ).fetchone()[0]
            == 1
        )
    async with asyncio.timeout(20):
        reconnected = await agent.load_session(str(native.project), sid, mcp_servers=[])
        reconnect_cursor = cursor_envelope(reconnected.field_meta)
        assert reconnect_cursor.scope == current.scope
        assert reconnect_cursor.status == "proven"
        assert reconnect_cursor.observation.cursor == current.observation.cursor
        assert await agent.inputs.drain_inbox(sid) == 0
    assert native.provider.posts == calls
    assert agent.inputs.pending_turns == {}
    assert not (comms.root / "acks.json").exists()


async def test_delayed_old_cursor_update_cannot_rebind_new_owner_snapshot(tmp_path, monkeypatch):
    """ACP metadata orders a delayed epoch-2 update below trusted epoch-3 load.

    This tests the backend contract, not a Toad widget or provider acceptance.
    """
    comms, agent, _ = _session(tmp_path)
    monkeypatch.setattr(cohort_foreground, "_trusted_package", lambda _: None)
    monkeypatch.setattr(coordinated_runtime, "_trusted_package", lambda _: None)
    fake, calls = _fake_model(decision="FULL")
    monkeypatch.setattr(TrackedTurnSession, "execute", fake)
    invoke_tool(comms, "comms_send", {"from": "sender", "to": "beta", "body": "selected"})
    entered = asyncio.Event()
    release = asyncio.Event()
    delivered = []

    async def update(*, session_id, update):
        assert session_id == "beta"
        cursor = cursor_fact(update.field_meta)
        if cursor and cursor.envelope.status == "proven":
            entered.set()
            await release.wait()
        delivered.append(update)

    async def noop(*_args, **_kwargs):
        return None

    async def no_options(*_args, **_kwargs):
        return []

    monkeypatch.setattr(agent._runtime, "session_update", update)
    monkeypatch.setattr(agent._runtime, "start", noop)
    monkeypatch.setattr(agent.inputs, "ensure_live_drain", lambda _: None)
    monkeypatch.setattr(agent.sessions.config, "options", no_options)
    drain = asyncio.create_task(agent.inputs.drain_inbox("beta"))
    await asyncio.wait_for(entered.wait(), timeout=5)
    comms.registry.unregister("beta")
    comms.registry.heartbeat("beta")
    loaded = await agent.load_session(str(tmp_path), "beta", mcp_servers=[])
    fresh = cursor_envelope(loaded.field_meta)
    assert fresh.status == "none"
    assert fresh.scope.admission_generation > 0
    release.set()
    assert await asyncio.wait_for(drain, timeout=5) == 1
    old = cursor_envelope(delivered[-1].field_meta)
    assert old.status == "proven" and old.observation.cursor.input_id
    assert old.scope.admission_generation < fresh.scope.admission_generation
    assert old.revision < fresh.revision
    assert old.scope.session_id == fresh.scope.session_id == "beta"
    assert len(calls) == 1


async def test_observed_mid_session_admission_change_invalidates_old_proof(tmp_path, monkeypatch):
    """No reattach is required for a registry stop/heartbeat admission bump."""
    comms, agent, _ = _session(tmp_path)
    monkeypatch.setattr(cohort_foreground, "_trusted_package", lambda _: None)
    monkeypatch.setattr(coordinated_runtime, "_trusted_package", lambda _: None)
    fake, calls = _fake_model(decision="FULL")
    monkeypatch.setattr(TrackedTurnSession, "execute", fake)
    updates = []

    async def record_update(*, session_id, update):
        assert session_id == "beta"
        cursor = cursor_fact(update.field_meta)
        if cursor is not None:
            updates.append(cursor.envelope)

    monkeypatch.setattr(agent._runtime, "session_update", record_update)
    invoke_tool(comms, "comms_send", {"from": "sender", "to": "beta", "body": "first"})
    assert await agent.inputs.drain_inbox("beta") == 1
    proven = cursor_envelope((await agent.sessions.metadata("beta")))
    assert proven.status == "proven"
    comms.registry.unregister("beta")
    assert await agent.inputs.drain_inbox("beta") == 0
    assert updates[-1].status == "unavailable" and updates[-1].scope is None
    comms.registry.heartbeat("beta")
    assert await agent.inputs.drain_inbox("beta") == 0
    renewed = updates[-1]
    assert renewed.status == "none" and not isinstance(
        renewed.observation, VerifiedCursorObservation
    )
    assert renewed.scope.admission_generation > proven.scope.admission_generation
    assert renewed.revision > proven.revision
    invoke_tool(comms, "comms_send", {"from": "sender", "to": "beta", "body": "second"})
    assert await agent.inputs.drain_inbox("beta") == 1
    assert updates[-1].status == "none"
    assert updates[-1].scope == renewed.scope
    assert len(calls) == 2  # no old-epoch native input can initialize new proof


async def test_unavailable_cursor_metadata_retains_owner_scope(tmp_path):
    comms, agent, _ = _session(tmp_path)
    before = cursor_envelope((await agent.sessions.metadata("beta")))
    with Coordination(str(comms.root / "coordination.sqlite3")) as store:
        store.session._connection.execute("DROP TABLE native_runtime_schema_meta")
    unavailable = cursor_envelope((await agent.sessions.metadata("beta")))
    assert unavailable.status == "unavailable"
    assert unavailable.scope == before.scope
    assert unavailable.revision > before.revision
    assert not isinstance(unavailable.observation, VerifiedCursorObservation)


async def test_cursor_refresh_defers_real_lock_contention_but_not_invalid_proof(
    tmp_path, monkeypatch
):
    comms, agent, _ = _session(tmp_path)
    monkeypatch.setattr(cohort_foreground, "_trusted_package", lambda _: None)
    monkeypatch.setattr(coordinated_runtime, "_trusted_package", lambda _: None)
    fake, calls = _fake_model()
    monkeypatch.setattr(TrackedTurnSession, "execute", fake)
    updates = []

    async def record_update(*, session_id, update):
        if cursor := cursor_fact(update.field_meta):
            updates.append(cursor.envelope)

    monkeypatch.setattr(agent._runtime, "session_update", record_update)
    comms.messaging.send_message("sender", "beta", "first")
    assert await agent.inputs.drain_inbox("beta") == 1
    assert updates[-1].status == "proven"
    before = len(updates)
    for _ in range(3):
        loaded = cursor_envelope((await agent.sessions.metadata("beta")))
        assert loaded.same_observation(updates[-1])
        await agent.cursors.refresh("beta", "beta")
        assert len(updates) == before, "Unchanged trusted reads republished the same cursor"
    with _store_lock(comms.root / "wire"):
        await agent.cursors.publish("beta", "beta")
    await agent.cursors.publish("beta", "beta")
    assert len(updates) == before, "Periodic contention forgot unchanged announced authority"
    with _store_lock(comms.root / "wire"):
        # This is an actual contended flock in the canonical read path, not a
        # mocked error. A new attachment cannot claim an unread observation.
        loaded = next(
            update.envelope
            for update in (await agent.cursors.trusted_metadata("beta", "beta"))
            if isinstance(update, CursorAdvancedUpdate)
        )
        assert loaded.status == "unavailable"
        await agent.cursors.publish("beta", "beta")
    assert len(updates) == before
    await agent.cursors.publish("beta", "beta")
    # The trusted load invalidates a different announced observation, so the
    # recovered proof must still publish after the lock clears.
    assert len(updates) == before + 1
    assert updates[-1].same_observation(updates[-2])
    assert updates[-1].revision > loaded.revision
    with Coordination(str(comms.root / "coordination.sqlite3")) as store:
        store.session._connection.execute("DROP TABLE native_runtime_schema_meta")
    await agent.cursors.publish("beta", "beta")
    assert updates[-1].status == "unavailable"
    assert updates[-1].scope == loaded.scope
    before = len(updates)
    await agent.cursors.refresh("beta", "beta")
    await agent.cursors.refresh("beta", "beta")
    assert len(updates) == before, "Unchanged unavailable observation was republished"
    assert len(calls) == 1  # Observation never initiates or replays an input.


async def test_contended_cursor_refresh_still_invalidates_replaced_owner(tmp_path, monkeypatch):
    comms, agent, _ = _session(tmp_path)
    updates = []

    async def record_update(*, session_id, update):
        if cursor := cursor_fact(update.field_meta):
            updates.append(cursor.envelope)

    monkeypatch.setattr(agent._runtime, "session_update", record_update)
    await agent.cursors.publish("beta", "beta")
    scope = updates[-1].scope

    def replacement_during_read(*args, **kwargs):
        comms.registry.unregister("beta")
        comms.registry.heartbeat("beta")
        raise BlockingIOError("writer holds the observation lock")

    monkeypatch.setattr(
        "agent_comms.cursor_publication.NativeSourceCursor.read", replacement_during_read
    )
    await agent.cursors.publish("beta", "beta")
    assert updates[-1].status == "unavailable"
    assert updates[-1].scope.admission_generation > scope.admission_generation


async def test_acp_private_does_not_overlap_owner_turn(native_backend):
    from agent_comms.field_codec import FieldCodec
    from agent_comms.messages import Message, MessageType
    from agent_comms.native_input_record import FullNativeExecution
    from agent_comms.wake import derive_exact_reply_target

    native = native_backend
    await native.author_history()
    async with native.open_owner(runtime_enabled=True, auto_wake=True) as (agent, session):
        comms = agent._comms
        comms.threads.claim_thread("sender", tags=frozenset(), worktree=str(native.project))
        async with native.original_input(agent, session, "Current human input") as turn:
            sent = comms.messaging.send_message("sender", session, "Selected input after current turn")
            native.provider.text = json.dumps(FieldCodec.encode((Message(
                session, derive_exact_reply_target(sent), "Selected input acknowledged",
                MessageType.INFO, timestamp=0,
            ),)))
            assert await agent.inputs.drain_inbox(session) == 0
            assert agent.turns.owns_turn(session, turn.turn_id)
            assert native.provider.posts == 0
        # The original lease's retirement, not a test map mutation, enables
        # canonical selected execution on the same SDK-authored saved source.
        # This direct original requires FULL; it is not a relevance probe.
        # Runtime observation is an actual concurrent consumer; whichever
        # original drain owns the admission consumes the original assignment.
        await agent.inputs.drain_inbox(session)
        async with asyncio.timeout(30):
            while native.provider.posts != 1 or agent.turns.turn_state(session).busy:
                await asyncio.sleep(0.01)
        assert native.provider.posts == 1
        with Coordination(str(comms.root / "coordination.sqlite3")) as store, store.session.read():
            (original,) = NativeRuntimeInput.select(
                store.session._connection, where="owner_thread=?", parameters=(session,),
            )
            assert original.stage is FullNativeExecution
            assert original.require_context_proof().input_id == original.input_id
        assert comms.bus.log.message_by_id(sent.message_id) == sent
        assert not agent.turns.turn_state(session).busy and not agent.inputs.backend_inboxes


async def test_acp_private_without_explicit_package_refuses_legacy_delivery(tmp_path):
    comms, agent, root_id = _session(tmp_path, package=False)
    sent = invoke_tool(comms, "comms_send", {"from": "sender", "to": "beta", "body": "selected"})
    original = comms.bus.log.message_by_id(sent["id"])
    with pytest.raises(PublicationActivationBlocked, match="explicit matching root"):
        await agent.inputs.drain_inbox("beta")
    with Coordination(str(comms.root / "coordination.sqlite3")) as store:
        assert (
            store.session._connection.execute(
                "SELECT COUNT(*) FROM claim_batch_receipts WHERE wire_root_id=? AND wire_seq=?",
                (root_id, original.seq),
            ).fetchone()[0]
            == 0
        )
    assert agent.inputs.pending_turns == {}


async def test_acp_private_no_wake_has_delivery_receipt_but_no_model(tmp_path, monkeypatch):
    comms, agent, root_id = _session(tmp_path)
    alpha = Thread(
        "alpha",
        frozenset({"team"}),
        str(tmp_path),
        process_identity=ProcessIdentity.capture(os.getpid()),
    )
    comms.registry.declare(alpha)
    with Coordination(str(comms.root / "coordination.sqlite3")) as store:
        store.participants.register(
            stable_thread_lookup(alpha.created_at), "alpha", "alpha", committed=True
        )
    monkeypatch.setattr(cohort_foreground, "_trusted_package", lambda _: None)
    monkeypatch.setattr(coordinated_runtime, "_trusted_package", lambda _: None)
    fake, calls = _fake_model(decision="FULL")
    monkeypatch.setattr(TrackedTurnSession, "execute", fake)
    sent = invoke_tool(
        comms,
        "comms_send",
        {"from": "sender", "to": "#team", "body": "@alpha review this."},
    )
    original = comms.bus.log.message_by_id(sent["id"])
    assert await agent.inputs.drain_inbox("beta") == 0
    assert calls == []
    cursor = cursor_envelope((await agent.sessions.metadata("beta")))
    assert cursor.status == "coverage_only"
    assert cursor.observation.cursor.covered_seq == original.seq
    assert (
        cursor.observation.cursor.injected_seq == 0 and cursor.observation.cursor.input_id is None
    )
    assert await agent.inputs.drain_inbox("beta") == 0
    assert calls == []
    with Coordination(str(comms.root / "coordination.sqlite3")) as store:
        receipt = store.session._connection.execute(
            "SELECT kind,claim_id FROM cohort_delivery_receipts WHERE wire_root_id=? "
            "AND wire_seq=? AND recipient_lookup=?",
            (
                root_id,
                original.seq,
                stable_thread_lookup(comms.registry.require("beta").created_at),
            ),
        ).fetchone()
        assert tuple(receipt) == ("unmentioned_observer", None)


async def test_acp_uncertain_native_turn_is_not_replayed_or_acked(tmp_path, monkeypatch):
    comms, agent, _ = _session(tmp_path)
    monkeypatch.setattr(cohort_foreground, "_trusted_package", lambda _: None)
    monkeypatch.setattr(coordinated_runtime, "_trusted_package", lambda _: None)
    fake, calls = _fake_model(fail_on=1)
    monkeypatch.setattr(TrackedTurnSession, "execute", fake)
    sent = invoke_tool(comms, "comms_send", {"from": "sender", "to": "beta", "body": "selected"})
    assert comms.bus.log.message_by_id(sent["id"]) is not None
    with pytest.raises(NativePiUnavailable):
        await agent.inputs.drain_inbox("beta")
    assert len(calls) == 1
    assert await agent.inputs.drain_inbox("beta") == 0
    assert len(calls) == 1
    assert agent.inputs.pending_turns == {}
    with Coordination(str(comms.root / "coordination.sqlite3")) as store:
        rows = NativeRuntimeInput.select(store.session._connection)
        assert len(rows) == 1 and rows[0].session_id is None


async def test_two_acp_instances_cannot_engage_or_send_simultaneously(tmp_path, monkeypatch):
    comms, first, root_id = _session(tmp_path)
    second = CommsAgent(
        comms,
        runtime_enabled=True,
        private_nk_native_package=tmp_path,
        private_nk_wire_root_id=root_id,
    )
    second.sessions.bindings["beta"] = "beta"
    second.sessions.titles["beta"] = "beta"
    second.sessions.worktrees["beta"] = str(tmp_path)
    monkeypatch.setattr(cohort_foreground, "_trusted_package", lambda _: None)
    monkeypatch.setattr(coordinated_runtime, "_trusted_package", lambda _: None)
    fake, calls = separate_session_fake(decision="FULL")
    entered, release = asyncio.Event(), asyncio.Event()

    async def suspended(package, **kwargs):
        result = await fake(package, **kwargs)
        if len(calls) == 1:
            entered.set()
            await release.wait()
        return result

    monkeypatch.setattr(TrackedTurnSession, "execute", suspended)
    for body in ("one", "two"):
        invoke_tool(comms, "comms_send", {"from": "sender", "to": "beta", "body": body})
    running = asyncio.create_task(first.inputs.drain_inbox("beta"))
    await asyncio.wait_for(entered.wait(), timeout=5)
    try:
        with pytest.raises(StaleFence, match="busy"):
            await second.inputs.drain_inbox("beta")
        assert len(calls) == 1
        with Coordination(str(comms.root / "coordination.sqlite3")) as store:
            assert (
                store.session._connection.execute(
                    "SELECT COUNT(*) FROM wake_claims WHERE disposition='engaged'"
                ).fetchone()[0]
                == 1
            )
            assert len(NativeRuntimeInput.select(store.session._connection)) == 1
    finally:
        release.set()
        assert await asyncio.wait_for(running, timeout=5) == 1
    assert await second.inputs.drain_inbox("beta") == 1
    assert len(calls) == 2
    with Coordination(str(comms.root / "coordination.sqlite3")) as store:
        assert (
            store.session._connection.execute(
                "SELECT COUNT(*) FROM wake_claims WHERE disposition='engaged'"
            ).fetchone()[0]
            == 0
        )
        assert (
            store.session._connection.execute(
                "SELECT COUNT(*) FROM wake_claims WHERE disposition='completed'"
            ).fetchone()[0]
            == 2
        )


async def test_human_owner_turn_cannot_be_borrowed_by_private_acp(tmp_path, monkeypatch):
    comms, agent, _ = _session(tmp_path)
    monkeypatch.setattr(cohort_foreground, "_trusted_package", lambda _: None)
    monkeypatch.setattr(coordinated_runtime, "_trusted_package", lambda _: None)
    fake, calls = _fake_model(decision="FULL")
    monkeypatch.setattr(TrackedTurnSession, "execute", fake)
    invoke_tool(comms, "comms_send", {"from": "sender", "to": "beta", "body": "selected"})
    comms.agents.begin_turn("beta", "human-live-turn")
    with pytest.raises(StaleFence, match="busy"):
        await agent.inputs.drain_inbox("beta")
    with Coordination(str(comms.root / "coordination.sqlite3")) as store:
        assert (
            store.session._connection.execute(
                "SELECT COUNT(*) FROM claim_batch_receipts"
            ).fetchone()[0]
            == 0
        )
    assert calls == []
    comms.agents.finish_turn(comms.registry.require("beta").turn_lease)
    assert await agent.inputs.drain_inbox("beta") == 1
    assert len(calls) == 1


async def test_goal_change_between_reservation_and_native_send_refuses(tmp_path, monkeypatch):
    comms, agent, _ = _session(tmp_path)
    monkeypatch.setattr(cohort_foreground, "_trusted_package", lambda _: None)
    monkeypatch.setattr(coordinated_runtime, "_trusted_package", lambda _: None)
    fake, calls = _fake_model(decision="FULL")
    entered, release = asyncio.Event(), asyncio.Event()

    async def suspended_before_send(package, **kwargs):
        entered.set()
        await release.wait()
        return await fake(package, **kwargs)

    monkeypatch.setattr(TrackedTurnSession, "execute", suspended_before_send)
    invoke_tool(comms, "comms_send", {"from": "sender", "to": "beta", "body": "selected"})
    running = asyncio.create_task(agent.inputs.drain_inbox("beta"))
    await asyncio.wait_for(entered.wait(), timeout=5)
    try:
        goal = comms.goals.update_goal("beta", SetGoalAction(text="Work on a separate task"))
        assert goal.state.active
    finally:
        release.set()
    with pytest.raises(StaleFence, match="owner changed before native send"):
        await asyncio.wait_for(running, timeout=5)
    assert calls == []
    assert await agent.inputs.drain_inbox("beta") == 0
    with Coordination(str(comms.root / "coordination.sqlite3")) as store:
        rows = NativeRuntimeInput.select(store.session._connection)
        assert len(rows) == 1 and rows[0].session_id is None


async def test_stable_existing_goal_allows_separate_selected_direct_reply(tmp_path, monkeypatch):
    comms, agent, _ = _session(tmp_path)
    monkeypatch.setattr(cohort_foreground, "_trusted_package", lambda _: None)
    monkeypatch.setattr(coordinated_runtime, "_trusted_package", lambda _: None)
    fake, calls = _fake_model(decision="FULL")
    monkeypatch.setattr(TrackedTurnSession, "execute", fake)
    original = comms.goals.update_goal("beta", SetGoalAction(text="Separate ongoing goal"))
    invoke_tool(comms, "comms_send", {"from": "sender", "to": "beta", "body": "selected"})
    assert await agent.inputs.drain_inbox("beta") == 1
    assert len(calls) == 1
    assert comms.registry.require("beta").goal == original


async def test_acp_mismatched_root_and_bad_package_cannot_accept_claim(tmp_path, monkeypatch):
    comms, agent, root_id = _session(tmp_path)
    sent = invoke_tool(comms, "comms_send", {"from": "sender", "to": "beta", "body": "selected"})
    original = comms.bus.log.message_by_id(sent["id"])
    agent._private_nk_wire_root_id = "f" * 32
    with pytest.raises(PublicationActivationBlocked, match="root does not match configuration"):
        await agent.inputs.drain_inbox("beta")
    agent._private_nk_wire_root_id = root_id
    # Preflight must run before any SQL acceptance, not only before reservation.
    monkeypatch.setattr(
        cohort_foreground,
        "_trusted_package",
        lambda _: (_ for _ in ()).throw(PublicationActivationBlocked("unreviewed Pi")),
    )
    with pytest.raises(PublicationActivationBlocked, match="unreviewed Pi"):
        await agent.inputs.drain_inbox("beta")
    with Coordination(str(comms.root / "coordination.sqlite3")) as store:
        assert (
            store.session._connection.execute(
                "SELECT COUNT(*) FROM claim_batch_receipts WHERE wire_root_id=? AND wire_seq=?",
                (root_id, original.seq),
            ).fetchone()[0]
            == 0
        )
