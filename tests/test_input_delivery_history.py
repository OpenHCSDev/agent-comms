"""Current owner queues scope notices without altering durable input evidence."""

import os
from dataclasses import replace

import pytest

from agent_comms.acp import CommsAgent
from agent_comms.comms import wire
from agent_comms.input_disposition import InputDispositions
from agent_comms.runtime import RuntimeProxy, socket_path
from agent_comms.threads import Thread


def seed(comms, name):
    owners = frozenset({name})
    ledger = InputDispositions(comms.root / InputDispositions.filename)
    for key, seq in [("bus:1", 1), ("bus:2", 2), ("bus:7", 7), ("bus:8", 8), ("acp:ui", None)]:
        ledger.record(key, seq=seq, owner=name, admission=1, target=name, text=f"Text {key}")
    ledger.bind("bus:2", admission=1, turn_id="t", native_id="a" * 32, text="Text bus:2")
    ledger.review_for_goal(("bus:1",), owners=owners, goal_id="goal", goal_revision=1, turn_id="t")
    return ledger


def test_history_clear_is_notice_only_and_survives_rename_and_reopen(tmp_path):
    comms = wire(tmp_path)
    comms.registry.declare(Thread("owner", frozenset(), str(tmp_path)))
    comms.registry.declare(Thread("peer", frozenset(), str(tmp_path)))
    ledger = seed(comms, "owner")
    awaiting = frozenset({"bus:2", "bus:8", "acp:ui"})
    ledger.record("bus:3", seq=3, owner="peer", admission=1, target="peer", text="Peer")
    before = ledger.read().unknown(frozenset({"owner"}))
    files = {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    overview = comms.goals.input_delivery("owner", awaiting_keys=awaiting)
    assert [row["inputId"] for row in overview["inputs"]] == ["bus:2", "bus:8", "ui"]
    assert overview["historicalCount"] == 2
    assert overview["dismissedHistoricalCount"] == 0
    assert overview["historicalInputs"] == []
    assert {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()} == files
    detail = comms.goals.input_delivery("owner", include_history=True, awaiting_keys=awaiting)
    assert [r["inputId"] for r in detail["historicalInputs"]] == ["bus:1", "bus:7"]
    assert not any(r["noticeDismissed"] for r in detail["historicalInputs"])
    comms.registry.rename("owner", "renamed")
    assert comms.goals.input_delivery("renamed", awaiting_keys=awaiting) == overview
    result = comms.goals.dismiss_historical_inputs("renamed", awaiting_keys=awaiting)
    assert result["historicalCount"] == 0 and result["dismissedHistoricalCount"] == 2
    assert result["inputs"] == overview["inputs"]
    assert not ledger.read().rows["bus:3"].notice_dismissed
    assert (
        tuple(
            replace(row, notice_dismissed=False)
            for row in ledger.read().unknown(frozenset({"owner"}))
        )
        == before
    )
    reopened = wire(tmp_path)
    assert reopened.goals.input_delivery("renamed", awaiting_keys=awaiting) == result
    assert reopened.goals.unresolved_inputs("renamed") == [r.public() for r in before]
    assert all(
        r["noticeDismissed"]
        for r in reopened.goals.input_delivery(
            "renamed", include_history=True, awaiting_keys=awaiting
        )["historicalInputs"]
    )
    assert reopened.goals.dismiss_historical_inputs("renamed", awaiting_keys=awaiting) == result
    assert ledger.bind("bus:7", admission=1, turn_id="t2", native_id="b" * 32, text="Text bus:7")
    assert "bus:7" not in [
        r["inputId"]
        for r in reopened.goals.input_delivery("renamed", awaiting_keys=awaiting)["inputs"]
    ]
    assert ledger.read().rows["bus:7"].native_id == "b" * 32


def test_no_owner_queue_observation_never_dismisses_inputs(tmp_path):
    comms = wire(tmp_path)
    comms.registry.declare(Thread("owner", frozenset(), str(tmp_path)))
    ledger = InputDispositions(tmp_path / InputDispositions.filename)
    ledger.record("bus:1", seq=1, owner="owner", admission=1, target="owner", text="Hello")
    before = ledger.path.read_bytes()
    assert comms.goals.dismiss_historical_inputs("owner")["historicalCount"] == 0
    assert len(comms.goals.input_delivery("owner")["inputs"]) == 1
    assert ledger.path.read_bytes() == before
    assert comms.goals.input_delivery("owner")["currentScope"] == "unobserved"


@pytest.mark.skipif(os.name == "nt", reason="POSIX owner socket")
async def test_actual_owner_rpc_clears_notices_and_broadcasts_invalidation(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_COMMS_AGENT_MODELS", "test/model")
    comms = wire(tmp_path / "wire")
    owner = CommsAgent(comms, agent_bin="pi", runtime_enabled=True, auto_wake=False)
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
        await owner.inputs.replay_unknown_inputs(session, client=Client())
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
        assert not owner.inputs.pending_turns and not owner.inputs.wake_tasks
    finally:
        owner.sessions.client = None
        await proxy.close()
        await owner.shutdown()
