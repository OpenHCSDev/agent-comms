"""Explicit ACP session owner consumes ordinary private N/K without legacy ACK.

Only model responses are faked; this does not prove provider delivery or
activate private processing for existing public sessions.
"""

from __future__ import annotations


import os

import pytest

from agent_comms.thread_identity import RecordedThreadIncarnation
from agent_comms.acp import CommsAgent
from agent_comms.acp_extension import (
    CursorAdvancedUpdate,
    decode_updates,
)
from agent_comms.child_process import ProcessIdentity
from agent_comms.comms import Comms
from agent_comms.coordination_errors import ParticipantNotRegistered, PublicationActivationBlocked, StaleRevision
from agent_comms.coordinator import Coordination
from agent_comms.coordinated_runtime_schema import install_native_runtime_schema
from agent_comms.errors import RelationViolationError
from test_coordinated_runtime import tmp_path as private_root_fixture
from agent_comms.thread_model import DeclaredModel

tmp_path = private_root_fixture


def _private_comms(tmp_path):
    root = tmp_path / "wire"
    comms = Comms(root)
    comms.registry.declare(
        comms.registry.new_thread('sender', frozenset(), str(tmp_path), process_identity=ProcessIdentity.capture(os.getpid()))
    )
    owner = comms.registry.new_thread('beta', frozenset({'team'}), str(tmp_path), process_identity=ProcessIdentity.capture(os.getpid()), model=DeclaredModel('test/model'))
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
        assert after.owner_identity.owner_thread == "gamma"
        assert after.owner_identity.generation == before.owner_identity.generation
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
    with pytest.raises(RelationViolationError):
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


async def test_missing_native_schema_refuses_cursor_metadata(tmp_path):
    comms, agent, _ = _session(tmp_path)
    assert cursor_envelope((await agent.sessions.metadata("beta"))) is not None
    with Coordination(str(comms.root / "coordination.sqlite3")) as store:
        store.session._connection.execute("DROP TABLE native_runtime_triage_sources")
    with pytest.raises(PublicationActivationBlocked):
        await agent.sessions.metadata("beta")


