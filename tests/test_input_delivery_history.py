"""Current owner queues scope notices without altering durable input evidence."""

from dataclasses import replace

from agent_comms.comms import wire
from agent_comms.field_codec import GoalId, InputId, TurnId
from agent_comms.input_attempt import (
    OwnerQueueInputDeliveryScope,
    UnobservedInputDeliveryScope,
    UnresolvedInput,
)
from agent_comms.input_disposition import InputDispositions
from agent_comms.reported_turn import ReportedTurn
from agent_comms.threads import Thread
from stored_inputs import stored_inputs


def seed(comms, name):
    goal_id = GoalId.mint()
    turn_id = TurnId.mint()
    owners = frozenset({name})
    ledger = InputDispositions(comms.root)
    for key, seq in [("bus:1", 1), ("bus:2", 2), ("bus:7", 7), ("bus:8", 8), ("acp:ui", None)]:
        ledger.record(key, seq=seq, owner=name, claim_scope=1, target=name, text=f"Text {key}")
    ledger.bind(
        "bus:2",
        claim_scope=1,
        turn_id=TurnId('e3b98a4da31a127d4bde6e43033f66ba'),
        native_id=InputId("a" * 32),
        text="Text bus:2",
    )
    ledger.review_for_goal(
        ("bus:1",), owners=owners, goal_id=goal_id, goal_revision=1,
        turn_id=ReportedTurn(turn_id),
    )
    return ledger


def test_history_clear_is_notice_only_and_survives_rename_and_reopen(tmp_path):
    comms = wire(tmp_path)
    comms.registry.declare(Thread('owner', frozenset(), str(tmp_path)))
    comms.registry.declare(Thread('peer', frozenset(), str(tmp_path)))
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
    assert ledger.bind(
        "bus:7",
        claim_scope=1,
        turn_id=TurnId('c44474038d459e40e4714afefa7bf8da'),
        native_id=InputId("b" * 32),
        text="Text bus:7",
    )
    assert "bus:7" not in [
        r.input_id
        for r in reopened.goals.input_delivery("renamed", scope=scope).inputs
    ]
    assert stored_inputs(ledger.root).rows["bus:7"].native_id == "b" * 32


def test_no_owner_queue_observation_never_dismisses_inputs(tmp_path):
    comms = wire(tmp_path)
    comms.registry.declare(Thread('owner', frozenset(), str(tmp_path)))
    ledger = InputDispositions(tmp_path)
    ledger.record("bus:1", seq=1, owner="owner", claim_scope=1, target="owner", text="Hello")
    before = ledger.path.read_bytes()
    assert comms.goals.dismiss_historical_inputs("owner").historical_count == 0
    assert len(comms.goals.input_delivery("owner").inputs) == 1
    assert ledger.path.read_bytes() == before
    assert comms.goals.input_delivery("owner").current_scope is UnobservedInputDeliveryScope


