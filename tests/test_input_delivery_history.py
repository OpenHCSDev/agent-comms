"""Current owner queues scope notices without altering durable input evidence."""

from agent_comms.repo import Checkout
import io
import json
from contextlib import redirect_stdout
from dataclasses import replace

from agent_comms.cli import main
from agent_comms.input_key import AcpInputKey, BusInputKey
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


UI_KEY = AcpInputKey(InputId.mint())


def seed(comms, name):
    goal_id = GoalId.mint()
    turn_id = TurnId.mint()
    owners = frozenset({name})
    ledger = InputDispositions(comms.root)
    for key, seq in (
        (BusInputKey(1), 1), (BusInputKey(2), 2), (BusInputKey(7), 7),
        (BusInputKey(8), 8), (UI_KEY, None),
    ):
        ledger.record(key, seq=seq, owner=name, claim_scope=1, target=name, text=f"Text {key}")
    ledger.bind(
        BusInputKey(2),
        claim_scope=1,
        turn_id=TurnId('e3b98a4da31a127d4bde6e43033f66ba'),
        native_id=InputId("a" * 32),
        text="Text bus:2",
    )
    ledger.review_for_goal(
        (BusInputKey(1),), owners=owners, goal_id=goal_id, goal_revision=1,
        turn_id=ReportedTurn(turn_id),
    )
    return ledger


def test_history_clear_is_notice_only_and_survives_rename_and_reopen(tmp_path):
    comms = wire(tmp_path)
    comms.registry.declare(Thread('owner', frozenset(), Checkout.from_wire(str(tmp_path))))
    comms.registry.declare(Thread('peer', frozenset(), Checkout.from_wire(str(tmp_path))))
    ledger = seed(comms, "owner")
    scope = OwnerQueueInputDeliveryScope(frozenset({BusInputKey(2), BusInputKey(8), UI_KEY}))
    ledger.record(BusInputKey(3), seq=3, owner="peer", claim_scope=1, target="peer", text="Peer")
    document = stored_inputs(ledger.root)
    before = document.unknown(frozenset({"owner"}))
    overview = comms.goals.input_delivery("owner", scope=scope)
    assert [row.input_id for row in overview.inputs] == [BusInputKey(2), BusInputKey(8), UI_KEY]
    assert overview.historical_count == 2
    assert overview.dismissed_historical_count == 0
    assert overview.historical_inputs == ()
    assert stored_inputs(ledger.root) == document
    detail = comms.goals.input_delivery("owner", include_history=True, scope=scope)
    assert [r.input_id for r in detail.historical_inputs] == [BusInputKey(1), BusInputKey(7)]
    assert not any(r.notice_dismissed for r in detail.historical_inputs)
    comms.registry.rename("owner", "renamed")
    assert comms.goals.input_delivery("renamed", scope=scope) == overview
    result = comms.goals.dismiss_historical_inputs("renamed", scope=scope)
    assert result.historical_count == 0 and result.dismissed_historical_count == 2
    assert result.inputs == overview.inputs
    assert not stored_inputs(ledger.root).rows[BusInputKey(3)].notice_dismissed
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
        BusInputKey(7),
        claim_scope=1,
        turn_id=TurnId('c44474038d459e40e4714afefa7bf8da'),
        native_id=InputId("b" * 32),
        text="Text bus:7",
    )
    assert BusInputKey(7) not in [
        r.input_id
        for r in reopened.goals.input_delivery("renamed", scope=scope).inputs
    ]
    assert stored_inputs(ledger.root).rows[BusInputKey(7)].native_id == "b" * 32


def test_commander_reads_a_units_failed_inputs_through_the_tool(tmp_path):
    """2026-10-24: the commander hand-wrote SQL against the bus to list pi-mi's lost inputs."""
    comms = wire(tmp_path)
    comms.registry.declare(Thread('owner', frozenset(), Checkout.from_wire(str(tmp_path))))
    seed(comms, 'owner')
    output = io.StringIO()
    with redirect_stdout(output):
        code = main(['--root', str(comms.root), 'tool', '--tool', 'comms_input_delivery',
                     '--arguments', json.dumps({'thread': 'owner'})])
    assert code == 0, output.getvalue()
    overview = json.loads(output.getvalue())
    assert {row['sequence'] for row in overview['inputs']} >= {2, 8}


def test_no_owner_queue_observation_never_dismisses_inputs(tmp_path):
    comms = wire(tmp_path)
    comms.registry.declare(Thread('owner', frozenset(), Checkout.from_wire(str(tmp_path))))
    ledger = InputDispositions(tmp_path)
    ledger.record(BusInputKey(1), seq=1, owner="owner", claim_scope=1, target="owner", text="Hello")
    before = ledger.path.read_bytes()
    assert comms.goals.dismiss_historical_inputs("owner").historical_count == 0
    assert len(comms.goals.input_delivery("owner").inputs) == 1
    assert ledger.path.read_bytes() == before
    assert comms.goals.input_delivery("owner").current_scope is UnobservedInputDeliveryScope


