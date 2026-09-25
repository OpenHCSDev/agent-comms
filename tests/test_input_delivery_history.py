"""Delivery notices reflect durable migration boundaries, never invented receipts."""

import os

import pytest

from agent_comms import Thread, wire
from agent_comms.acp import CommsAgent
from agent_comms.input_disposition import AcpDeliveryCursors, InputDispositions
from agent_comms.runtime import RuntimeProxy, socket_path


def seed(comms, name):
    owners = frozenset({name})
    cursors = AcpDeliveryCursors(comms.root)
    cursors.initialize(owners, name, high_water=7, fresh=False)
    ledger = InputDispositions(comms.root)
    for key, seq in [("bus:1", 1), ("bus:2", 2), ("bus:7", 7), ("bus:8", 8), ("acp:ui", None)]:
        ledger.record(key, seq=seq, owner=name, admission=1, target=name, text=f"Text {key}")
    ledger.bind("bus:2", admission=1, turn_id="t", native_id="a" * 32, text="Text bus:2")
    ledger.review_for_goal(("bus:1",), owners=owners, goal_id="goal", goal_revision=1, turn_id="t")
    return ledger, cursors


def test_history_clear_is_notice_only_and_survives_rename_and_reopen(tmp_path):
    comms = wire(tmp_path)
    comms.register(Thread("owner", frozenset(), str(tmp_path)))
    comms.register(Thread("peer", frozenset(), str(tmp_path)))
    ledger, cursors = seed(comms, "owner")
    ledger.record("bus:3", seq=3, owner="peer", admission=1, target="peer", text="Peer")
    before = ledger.unknown(frozenset({"owner"}))
    files = {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    overview = comms.input_delivery("owner")
    assert [row["inputId"] for row in overview["inputs"]] == ["bus:2", "bus:8", "ui"]
    assert overview["historicalCount"] == 2
    assert overview["dismissedHistoricalCount"] == 0
    assert overview["historicalInputs"] == []
    assert {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()} == files
    detail = comms.input_delivery("owner", include_history=True)
    assert [r["inputId"] for r in detail["historicalInputs"]] == ["bus:1", "bus:7"]
    assert not any(r["noticeDismissed"] for r in detail["historicalInputs"])
    comms.registry.rename("owner", "renamed")
    assert comms.input_delivery("renamed") == overview
    result = comms.dismiss_historical_inputs("renamed")
    assert result["historicalCount"] == 0 and result["dismissedHistoricalCount"] == 2
    assert result["inputs"] == overview["inputs"]
    assert ledger.get("bus:3").get("notice_dismissed") is None
    assert [
        {k: v for k, v in row.items() if k != "notice_dismissed"}
        for row in ledger.unknown(frozenset({"owner"}))
    ] == before
    assert cursors.cursor(frozenset({"owner"})) == 0
    reopened = wire(tmp_path)
    assert reopened.input_delivery("renamed") == result
    assert reopened.unresolved_inputs("renamed") == [ledger.public(r) for r in before]
    assert all(
        r["noticeDismissed"]
        for r in reopened.input_delivery("renamed", include_history=True)["historicalInputs"]
    )
    assert reopened.dismiss_historical_inputs("renamed") == result
    assert ledger.bind("bus:7", admission=1, turn_id="t2", native_id="b" * 32, text="Text bus:7")
    assert "bus:7" in [r["inputId"] for r in reopened.input_delivery("renamed")["inputs"]]


def test_no_migration_boundary_never_dismisses_inputs(tmp_path):
    comms = wire(tmp_path)
    comms.register(Thread("owner", frozenset(), str(tmp_path)))
    ledger = InputDispositions(tmp_path)
    ledger.record("bus:1", seq=1, owner="owner", admission=1, target="owner", text="Hello")
    before = ledger.path.read_bytes()
    assert comms.dismiss_historical_inputs("owner")["historicalCount"] == 0
    assert len(comms.input_delivery("owner")["inputs"]) == 1
    assert ledger.path.read_bytes() == before
    assert not AcpDeliveryCursors(tmp_path).path.exists()


@pytest.mark.skipif(os.name == "nt", reason="POSIX owner socket")
async def test_actual_owner_rpc_clears_notices_and_broadcasts_invalidation(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_COMMS_AGENT_MODELS", "test/model")
    comms = wire(tmp_path / "wire")
    owner = CommsAgent(comms, agent_bin="pi", runtime_enabled=True, auto_wake=False)
    monkeypatch.setattr(owner, "_ensure_live_drain", lambda _: None)
    session = (await owner.new_session(str(tmp_path / "project"))).session_id
    # New sessions have no migration boundary. Replace the fixture cursor before seeding.
    AcpDeliveryCursors(comms.root).path.unlink()
    ledger, _ = seed(comms, session)
    updates = []

    class Client:
        async def session_update(self, **kwargs):
            updates.append(kwargs["update"])

    owner._client = Client()
    proxy = RuntimeProxy(owner, session, socket_path(comms.root, os.getpid()))
    before = comms.unresolved_inputs(session)
    try:
        await owner.replay_unknown_inputs(session, client=Client())
        assert [u.field_meta["agentComms"]["inputDisposition"]["inputId"] for u in updates] == [
            "bus:2",
            "bus:8",
            "ui",
        ]
        snapshot = await proxy.request("input_dispositions")
        assert snapshot["historicalCount"] == 2
        assert snapshot["historicalInputs"] == []
        with pytest.raises(RuntimeError, match="boolean"):
            await proxy.request("input_dispositions", include_history="yes")
        cleared = await proxy.request("dismiss_historical_inputs")
        assert cleared["historicalCount"] == 0 and len(cleared["inputs"]) == 3
        assert updates[-1].field_meta["agentComms"]["inputDeliveryChanged"] is True
        assert await proxy.request("input_dispositions") == cleared
        detailed = await proxy.request("input_dispositions", include_history=True)
        assert len(detailed["historicalInputs"]) == 2
        assert comms.unresolved_inputs(session) == before
        assert not owner._pending_turns and not owner._wake_tasks
    finally:
        owner._client = None
        await proxy.close()
        await owner.shutdown()
