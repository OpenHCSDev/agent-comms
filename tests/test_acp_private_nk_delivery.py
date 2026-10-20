"""Explicit ACP session owner consumes ordinary private N/K without legacy ACK.

Only model responses are faked; this does not prove provider delivery or
activate private processing for existing public sessions.
"""

from __future__ import annotations

from conftest import responder
from agent_comms.tool_caller import NoThreadCaller

import asyncio
import json
import os

import pytest

import agent_comms.coordinated_runtime as coordinated_runtime
import agent_comms.native_pi as native_pi
import agent_comms.private_nk_entrypoint as private_nk_entrypoint
from agent_comms.thread_identity import RecordedThreadIncarnation
from agent_comms.acp import CommsAgent
from agent_comms.acp_extension import (
    CursorAdvancedUpdate,
    VerifiedCursorObservation,
    decode_updates,
)
from agent_comms.child_process import ProcessIdentity
from agent_comms.activity import NativeInputDrainDiagnostic, UnavailableDrainDiagnostic
from agent_comms.cohort_foreground import _accept_visible_deliveries
from agent_comms.comms import Comms
from agent_comms.coordination_errors import ParticipantNotRegistered, PublicationActivationBlocked, StaleFence, StaleRevision
from agent_comms.coordinator import Coordination
from agent_comms.coordinated_runtime_schema import install_native_runtime_schema
from agent_comms.errors import RelationViolationError
from agent_comms.goal_actions import SetGoalAction
from agent_comms.native_runtime_input import NativeRuntimeInput
from agent_comms.native_session_reopen import NativeSessionIdentity
from agent_comms.native_package import VerifiedNativePackage
from agent_comms.participant_store import ParticipantStore
from agent_comms.pi_payloads import PublishedMessageToolDetails
from agent_comms.store_files import _store_lock
from agent_comms.threads import Thread
from agent_comms.tools import invoke_tool
from agent_comms.tracked_turn import TrackedTurnSession
from delivery_owner_fixture import drain_diagnostic, reply_turn_finished
from test_coordinated_runtime import _fake_model, tmp_path as private_root_fixture
from test_native_prompt_binding import _fake_model as separate_session_fake
from test_goal_standby_liveness import retained_native_acp_owner as retained_native_acp_owner

tmp_path = private_root_fixture
pytestmark = pytest.mark.usefixtures("fake_native_catalog")


def _private_comms(tmp_path):
    root = tmp_path / "wire"
    comms = Comms(root)
    comms.registry.declare(
        comms.registry.new_thread('sender', frozenset(), str(tmp_path), process_identity=ProcessIdentity.capture(os.getpid()))
    )
    owner = comms.registry.new_thread('beta', frozenset({'team'}), str(tmp_path), process_identity=ProcessIdentity.capture(os.getpid()), model='test/model')
    comms.registry.declare(owner)
    root_id = comms.messaging.initialize_private_initial_protocol()
    with Coordination(str(root / "coordination.sqlite3")) as store:
        store.participants.register(
            RecordedThreadIncarnation("fixture", owner.created_at).lookup, "beta", "beta", committed=True
        )
        install_native_runtime_schema(store)
    return comms, root_id


def _session(tmp_path):
    comms, root_id = _private_comms(tmp_path)
    agent = CommsAgent(comms, runtime_enabled=True)
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


@pytest.mark.parametrize("managed", [False, True])
def test_private_owner_rename_during_active_attempt_can_complete(tmp_path, managed):
    from agent_comms.attempt_start import AttemptStart
    from agent_comms.attempt_states import SucceededAttempt
    from agent_comms.coordination_tables.executions import ExecutionOrigin
    from agent_comms.owner_fence import prepare_fence_token
    from test_coordination_store import final_evidence

    comms, _ = _private_comms(tmp_path)
    lookup = RecordedThreadIncarnation("fixture", comms.registry.require("beta").created_at).lookup
    with Coordination(str(comms.root / "coordination.sqlite3")) as store:
        before = store.participants.get(lookup)
        store.executions.create("rename-active", ExecutionOrigin.ACP, lookup, "beta", 1)
        store.executions.mark_pending("rename-active", expected_revision=1)
        fence = store.attempts.start(AttemptStart(
            "rename-active", 1, "beta", 1, prepare_fence_token(),
            expected_execution_revision=2, expected_pointer_revision=0,
        )).value.fence
        active = store.participants.get(lookup)
    if managed:
        comms.threads.rename_managed_thread("beta", "gamma", owner_pid=os.getpid())
    else:
        comms.threads._rename_thread("beta", "gamma")
    assert comms.registry.require("beta").name == "gamma"
    with Coordination(str(comms.root / "coordination.sqlite3")) as store:
        after = store.participants.get(lookup)
        assert after.owner_thread == "gamma"
        assert after.participant_generation == before.participant_generation
        assert after.pointer == active.pointer
        with pytest.raises(StaleRevision):
            store.participants.rename_owner(lookup, "wrong", expected_generation=2)
        fence = final_evidence(store, fence)
        settled = store.attempts.settle_nonpublication(
            fence, expected_pointer_revision=1, outcome=SucceededAttempt(),
        ).value
        assert isinstance(settled.attempt.lifecycle, SucceededAttempt)
        assert not settled.is_current


def test_private_rename_refuses_mismatched_sql_owner_before_registry_mutation(tmp_path):
    comms, _ = _private_comms(tmp_path)
    lookup = RecordedThreadIncarnation("fixture", comms.registry.require("beta").created_at).lookup
    with Coordination(str(comms.root / "coordination.sqlite3")) as store:
        store.participants.advance_generation(lookup, "unexpected", expected_generation=1)
    with pytest.raises(RelationViolationError, match="Private coordinator owner differs"):
        comms.threads._rename_thread("beta", "gamma")
    assert comms.registry.require("beta").name == "beta"
    assert "gamma" not in comms.registry
    assert comms.bus.log.latest_sequence() == 0


def test_private_rename_of_unregistered_participant_takes_the_plain_path(tmp_path):
    comms, _ = _private_comms(tmp_path)
    lookup = RecordedThreadIncarnation("fixture", comms.registry.require("sender").created_at).lookup
    renamed = comms.threads._rename_thread("sender", "courier")
    assert (renamed.previous, renamed.current) == ("sender", "courier")
    assert comms.registry.require("sender").name == "courier"
    assert not (comms.root / ".private-owner-rename.pending").exists()
    with Coordination(str(comms.root / "coordination.sqlite3")) as store:
        with pytest.raises(ParticipantNotRegistered):
            store.participants.get(lookup)


async def test_acp_session_selected_native_pipeline_never_uses_legacy_ack(
    retained_native_acp_owner,
):
    native, comms, agent, sid = retained_native_acp_owner
    # The shared standby fixture disables autonomous wake. This explicit private
    # delivery journey enables the real owner policy, with its observer stopped.
    agent.inputs.auto_wake = True
    comms.registry.declare(
        comms.registry.new_thread('sender', frozenset(), str(native.project), process_identity=ProcessIdentity.capture(os.getpid()))
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
    sent = invoke_tool(comms, "comms_send", {"from": "sender", "to": sid, "body": "Compute 17+25"}, caller=NoThreadCaller())
    original, = comms.bus.log.messages_for_references((PublishedMessageToolDetails.from_wire(sent).message.source,))
    before = cursor_envelope((await agent.sessions.metadata(sid)))
    assert before.status == "none"
    assert before.revision >= 1
    assert before.scope.claim_scope.incarnation.name == sid
    assert before.scope.wire_root_id == root_id
    updates = []

    class Client:
        async def session_update(self, *, session_id, update):
            assert session_id == sid
            updates.append(update)

    agent.on_connect(Client())
    async with asyncio.timeout(30):
        assert await agent.inputs.drain_inbox(sid) == 1
        await reply_turn_finished(agent, sid)
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
    assert not any(state.pending_turns for state in agent.inputs.drained.values())
    assert not (comms.root / "acks.json").exists()


async def test_missing_native_schema_refuses_cursor_metadata(tmp_path):
    comms, agent, _ = _session(tmp_path)
    assert cursor_envelope((await agent.sessions.metadata("beta"))) is not None
    with Coordination(str(comms.root / "coordination.sqlite3")) as store:
        store.session._connection.execute("DROP TABLE native_runtime_schema_meta")
    with pytest.raises(PublicationActivationBlocked):
        await agent.sessions.metadata("beta")


