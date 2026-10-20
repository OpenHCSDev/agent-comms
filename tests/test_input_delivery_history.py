"""Current owner queues scope notices without altering durable input evidence."""

import os
from dataclasses import replace

import pytest

from agent_comms.comms import wire
from agent_comms.input_attempt import (
    OwnerQueueInputDeliveryScope, UnobservedInputDeliveryScope, UnresolvedInput,
)
from agent_comms.input_disposition import InputDispositions
from agent_comms.runtime import RuntimeProxy, socket_path
from agent_comms.threads import Thread
from stored_inputs import stored_inputs
from delivery_owner_fixture import canonical_agent


def seed(comms, name):
    owners = frozenset({name})
    ledger = InputDispositions(comms.root)
    for key, seq in [("bus:1", 1), ("bus:2", 2), ("bus:7", 7), ("bus:8", 8), ("acp:ui", None)]:
        ledger.record(key, seq=seq, owner=name, claim_scope=1, target=name, text=f"Text {key}")
    ledger.bind("bus:2", claim_scope=1, turn_id="t", native_id="a" * 32, text="Text bus:2")
    ledger.review_for_goal(("bus:1",), owners=owners, goal_id="goal", goal_revision=1, turn_id="t")
    return ledger


def test_history_clear_is_notice_only_and_survives_rename_and_reopen(tmp_path):
    comms = wire(tmp_path)
    comms.registry.declare(comms.registry.new_thread('owner', frozenset(), str(tmp_path)))
    comms.registry.declare(comms.registry.new_thread('peer', frozenset(), str(tmp_path)))
    ledger = seed(comms, "owner")
    scope = OwnerQueueInputDeliveryScope(frozenset({"bus:2", "bus:8", "acp:ui"}))
    ledger.record("bus:3", seq=3, owner="peer", claim_scope=1, target="peer", text="Peer")
    document = stored_inputs(ledger.root)
    before = document.unknown(frozenset({"owner"}))
    overview = comms.goals.input_delivery("owner", scope=scope)
    assert [row.input_id for row in overview.inputs] == ["bus:2", "bus:8", "ui"]
    assert overview.historical_count == 2
    assert overview.dismissed_historical_count == 0
    assert overview.historical_inputs == ()
    assert stored_inputs(ledger.root) == document
    detail = comms.goals.input_delivery("owner", include_history=True, scope=scope)
    assert [r.input_id for r in detail.historical_inputs] == ["bus:1", "bus:7"]
    assert not any(r.notice_dismissed for r in detail.historical_inputs)
    comms.registry.rename("owner", "renamed")
    assert comms.goals.input_delivery("renamed", scope=scope) == overview
    result = comms.goals.dismiss_historical_inputs("renamed", scope=scope)
    assert result.historical_count == 0 and result.dismissed_historical_count == 2
    assert result.inputs == overview.inputs
    assert not stored_inputs(ledger.root).rows["bus:3"].notice_dismissed
    assert (
        tuple(
            replace(row, notice_dismissed=False)
            for row in stored_inputs(ledger.root).unknown(frozenset({"owner"}))
        )
        == before
    )
    reopened = wire(tmp_path)
    assert reopened.goals.input_delivery("renamed", scope=scope) == result
    assert reopened.goals.unresolved_inputs("renamed") == tuple(UnresolvedInput.of(r) for r in before)
    assert all(
        r.notice_dismissed
        for r in reopened.goals.input_delivery(
            "renamed", include_history=True, scope=scope
        ).historical_inputs
    )
    assert reopened.goals.dismiss_historical_inputs("renamed", scope=scope) == result
    assert ledger.bind("bus:7", claim_scope=1, turn_id="t2", native_id="b" * 32, text="Text bus:7")
    assert "bus:7" not in [
        r.input_id
        for r in reopened.goals.input_delivery("renamed", scope=scope).inputs
    ]
    assert stored_inputs(ledger.root).rows["bus:7"].native_id == "b" * 32


def test_no_owner_queue_observation_never_dismisses_inputs(tmp_path):
    comms = wire(tmp_path)
    comms.registry.declare(comms.registry.new_thread('owner', frozenset(), str(tmp_path)))
    ledger = InputDispositions(tmp_path)
    ledger.record("bus:1", seq=1, owner="owner", claim_scope=1, target="owner", text="Hello")
    before = ledger.path.read_bytes()
    assert comms.goals.dismiss_historical_inputs("owner").historical_count == 0
    assert len(comms.goals.input_delivery("owner").inputs) == 1
    assert ledger.path.read_bytes() == before
    assert comms.goals.input_delivery("owner").current_scope is UnobservedInputDeliveryScope


@pytest.mark.skipif(os.name == "nt", reason="POSIX owner socket")
async def test_actual_owner_rpc_clears_notices_and_broadcasts_invalidation(
    tmp_path, monkeypatch, native_rpc_fixture,
):
    monkeypatch.setenv("AGENT_COMMS_AGENT_MODELS", "test/model")
    comms = wire(tmp_path / "wire")
    owner = canonical_agent(comms, agent_bin="pi", runtime_enabled=True, auto_wake=False)
    from goal_owner_fixture import activate_empty_source

    activate_empty_source(owner)
    monkeypatch.setattr(owner.inputs, "ensure_live_drain", lambda _: None)
    session = (await owner.new_session(str(tmp_path / "project"))).session_id
    seed(comms, session)
    updates = []

    class Client:
        async def session_update(self, **kwargs):
            updates.append(kwargs["update"])

    owner.sessions.client = Client()
    proxy = RuntimeProxy(owner, session, socket_path(comms.root, os.getpid()))
    before = comms.goals.unresolved_inputs(session)
    try:
        await owner.inputs.emit_input_delivery_changed(session, client=Client())
        assert updates == [], "An idle owner has no currently awaiting inputs to replay"
        snapshot = await proxy.request("input_dispositions")
        assert snapshot["historicalCount"] == 5
        assert snapshot["historicalInputs"] == []
        with pytest.raises(RuntimeError, match="Expected.*bool"):
            await proxy.request("input_dispositions", include_history="yes")
        cleared = await proxy.request("dismiss_historical_inputs")
        assert cleared["historicalCount"] == 0 and cleared["inputs"] == []
        assert updates[-1].field_meta["agentComms"]["inputDeliveryChanged"] is True
        assert await proxy.request("input_dispositions") == cleared
        detailed = await proxy.request("input_dispositions", include_history=True)
        assert len(detailed["historicalInputs"]) == 5
        assert comms.goals.unresolved_inputs(session) == before
        assert not any(state.pending_turns for state in owner.inputs.drained.values()) and not any(state.wake_task for state in owner.inputs.drained.values())
    finally:
        owner.sessions.client = None
        await proxy.close()
        await owner.shutdown()
