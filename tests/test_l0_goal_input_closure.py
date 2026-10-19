"""Current goal/input boundaries through durable stores and the real owner socket."""

import os
import sqlite3
from dataclasses import replace
from pathlib import Path

import pytest

from agent_comms.acp import CommsAgent
from agent_comms.child_process import ProcessIdentity
from agent_comms.comms import wire
from agent_comms.field_codec import FieldCodec
from agent_comms.goal_actions import (
    GoalPrecondition,
    OwnerInvocable,
    RetryGoalAction,
    SetGoalAction,
    StandbyGoalAction,
)
from agent_comms.goal_attempts import GoalAttemptStore, GoalHumanDecision
from agent_comms.goal_generation import ReadyGeneration
from agent_comms.goal_history import GoalHistoryEntry
from agent_comms.goal_states import BlockedGoal, GoalState, UnrecordedBlockGoal
from agent_comms.goal_waits import GoalWait, GoalWaits
from agent_comms.goals import Goal
from agent_comms.input_disposition import InputDispositions
from agent_comms.queued_input import QueuedInput
from agent_comms.runtime import RuntimeProxy, socket_path
from agent_comms.threads import Thread
from stored_inputs import stored_inputs


def register(comms, name):
    comms.registry.declare(
        comms.registry.new_thread(name, frozenset(), str(comms.root), process_identity=ProcessIdentity.capture(os.getpid()))
    )


def test_unknown_reason_survives_real_history_without_inventing_provenance(tmp_path):
    comms = wire(tmp_path)
    register(comms, "owner")
    goal = Goal(
        "Retained objective", "retained", published_seq=0, progress="Unrelated progress", state=UnrecordedBlockGoal()
    )
    comms.registry.register(replace(comms.registry.require("owner"), goal=goal))
    history = comms.goals.goal_history("owner")
    assert history[-1].after == goal and history[-1].after.mention_source is None
    assert history[-1].after.state.reason is None
    assert (
        comms.goals.goal_execution("owner").presentation("owner").summary
        == "Blocked · reason unavailable"
    )
    with sqlite3.connect(tmp_path / "goal_history.sqlite3") as database:
        before = database.execute(
            f"SELECT * FROM {GoalHistoryEntry.declared_name} ORDER BY sequence"
        ).fetchall()
    reopened = wire(tmp_path)
    assert reopened.goals.goal_history("owner") == history
    assert not reopened.relationships._goal_contacts(reopened.registry.snapshot())[0]
    with sqlite3.connect(tmp_path / "goal_history.sqlite3") as database:
        assert (
            database.execute(
                f"SELECT * FROM {GoalHistoryEntry.declared_name} ORDER BY sequence"
            ).fetchall()
            == before
        )
    for data in ({"kind": "blocked"}, {"kind": "blocked", "block_reason": None}):
        with pytest.raises((TypeError, ValueError)):
            FieldCodec.decode(GoalState, data)
    assert FieldCodec.decode(GoalState, {"kind": "unrecorded_block"}) == UnrecordedBlockGoal()


@pytest.mark.parametrize(
    "state", [BlockedGoal("Inspect the failed attempt"), UnrecordedBlockGoal()]
)
def test_retry_requires_existing_private_generation_and_never_adopts(tmp_path, state):
    comms = wire(tmp_path / "wire")
    register(comms, "owner")
    goal = Goal("Objective", "goal", published_seq=0, state=state)
    comms.registry.register(replace(comms.registry.require("owner"), goal=goal))
    store = comms.goals.open_goal_store()
    retry = RetryGoalAction(
        expect=GoalPrecondition(goal_id=goal.id, expected_owner=ProcessIdentity.capture(os.getpid()))
    )
    with pytest.raises(ValueError, match="Retry cannot create a grant"):
        comms.goals.update_goal("owner", retry, actor=OwnerInvocable)
    assert store.snapshot(goal.id) is None
    assert comms.registry.require("owner").goal == goal
    # A known failed generation requires an explicit owner decision to advance.
    store.create_goal(goal.id)
    reservation = store.reserve(goal.id, 1)
    reservation.fail(store, "Observed failure")
    assert not store.snapshot(goal.id).lifecycle.allows_resume(False)
    changed = comms.goals.update_goal("owner", retry, actor=OwnerInvocable)
    assert changed.state.active and changed.id == goal.id
    assert store.snapshot(goal.id).lifecycle == ReadyGeneration()
    assert store.snapshot(goal.id).number == 2
    with sqlite3.connect(store.path) as database:
        assert (
            database.execute(f"SELECT count(*) FROM {GoalHumanDecision.declared_name}").fetchone()[
                0
            ]
            == 1
        )


def test_standby_uses_native_receipts_and_explicit_reviews_not_read_ack(tmp_path):
    from goal_owner_fixture import canonical_goal_wire

    comms = canonical_goal_wire(tmp_path)
    register(comms, "owner")
    register(comms, "peer")
    comms.agents.begin_turn("peer", "peer-turn")
    goal = comms.goals.update_goal("owner", SetGoalAction(text="Wait for the peer"))
    message = comms.messaging.send("peer", "owner", "Review this uncertain reply").message
    store = InputDispositions(tmp_path)
    claim_scope = comms.registry.snapshot().claimed_generations["owner"]
    key = f"bus:{message.seq}"
    store.record(
        key, seq=message.seq, owner="owner", claim_scope=claim_scope, target="owner", text=message.body
    )
    messages = comms.bus.inbox("owner")
    comms.messaging.acknowledge("owner", tuple(message.reference for message in messages))
    action = StandbyGoalAction(expect=GoalPrecondition(goal_id=goal.id), wait_for=("peer",))
    with pytest.raises(ValueError, match="pending or UNKNOWN"):
        comms.goals.update_goal("owner", action)
    review = comms.goals.goal_input_review("owner", goal.id, ["peer"])
    assert list(review.reviewed_inputs) == [key]
    comms.goals.update_goal("owner", replace(action, reviewed_inputs=(key,)))
    row = stored_inputs(store.root).rows[key]
    assert row.accepts_reservation and row.reviewed_for_goal(goal.id)
    assert not (tmp_path / "acp_delivery_cursors.json").exists()
    # A later native-started reply is already handled, regardless of display reads.
    second = comms.messaging.send("peer", "owner", "Native reply").message
    second_key = f"bus:{second.seq}"
    store.record(
        second_key,
        seq=second.seq,
        owner="owner",
        claim_scope=claim_scope,
        target="owner",
        text=second.body,
    )
    assert store.bind(
        second_key, claim_scope=claim_scope, turn_id="native-turn", native_id="a" * 32, text=second.body
    )
    assert store.started(second_key, turn_id="native-turn", native_id="a" * 32, text=second.body)
    comms.goals.update_goal("owner", action)
    wait = comms.goals.goal_wait("owner")
    assert wait.owner == comms.registry.require("owner").incarnation
    assert wait.report_turn_id is None and wait.report_turn_generation is None
    saved = FieldCodec.encode(wait)
    for missing in (
        "owner",
        "target_turn_generations",
        "report_turn_id",
        "report_turn_generation",
    ):
        data = dict(saved)
        del data[missing]
        with pytest.raises((TypeError, ValueError)):
            FieldCodec.decode(GoalWait, data)
    assert GoalWaits(tmp_path / "goal_waits.sqlite3").read()[goal.id] == wait


@pytest.mark.skipif(os.name == "nt", reason="POSIX owner socket")
async def test_real_owner_socket_preserves_unknown_and_refuses_grant_adoption(
    tmp_path, monkeypatch, fake_native_catalog
):
    from agent_comms.native_arguments import NativeArguments

    arguments = ("--model", "test/model")
    configured_model = NativeArguments.configured(arguments).model
    assert configured_model is not None
    monkeypatch.setenv("AGENT_COMMS_AGENT_MODELS", configured_model)
    comms = wire(tmp_path / "wire")
    owner = CommsAgent(
        comms, agent_bin="pi", agent_args=arguments, runtime_enabled=True, auto_wake=False
    )
    from goal_owner_fixture import activate_empty_source

    activate_empty_source(owner)
    session = (await owner.new_session(str(tmp_path / "project"))).session_id
    proxy = RuntimeProxy(owner, session, socket_path(comms.root, os.getpid()))
    try:
        ledger = owner.inputs.dispositions
        claim_scope = comms.registry.snapshot().claimed_generations[session]
        async with QueuedInput.reserve(
            owner.inputs, session, session, text="queued", prompt="queued", echo=True,
            images=(), controller=None,
        ) as reservation:
            queued = reservation.item
            owner.inputs.state(session).following = {queued.input_id: queued.source()}
            reservation.release()
        ledger.record(
            "acp:uncertain", seq=None, owner=session, claim_scope=claim_scope,
            target=session, text="uncertain",
        )
        ledger.bind(
            "acp:uncertain",
            claim_scope=claim_scope,
            turn_id="finished-owner-turn",
            native_id="b" * 32,
            text="uncertain",
        )
        before = stored_inputs(ledger.root).rows
        current = await proxy.request("input_dispositions")
        assert [row["inputId"] for row in current["inputs"]] == [queued.input_id]
        assert current["historicalCount"] == 1
        cleared = await proxy.request("dismiss_historical_inputs")
        assert cleared["dismissedHistoricalCount"] == 1 and cleared["inputs"] == current["inputs"]
        assert {
            key: replace(row, notice_dismissed=False) for key, row in stored_inputs(ledger.root).rows.items()
        } == before
        assert not any(state.pending_turns for state in owner.inputs.drained.values()) and not any(state.wake_task for state in owner.inputs.drained.values())
        goal = Goal(
            "Retained goal without private authority", "missing", published_seq=0, state=UnrecordedBlockGoal()
        )
        comms.registry.register(replace(comms.registry.require(session), goal=goal))
        with pytest.raises(RuntimeError, match="Retry cannot create a grant"):
            await proxy.request("retry_goal", goal_id=goal.id, expected_revision=goal.revision)
        assert comms.goals.open_goal_store().snapshot(goal.id) is None
        assert comms.registry.require(session).goal == goal
        assert not owner.turns.turn_tasks and not any(state.wake_task for state in owner.inputs.drained.values())
    finally:
        await proxy.close()
        await owner.shutdown()


@pytest.mark.refactor_guard
def test_retired_goal_input_authorities_cannot_return():
    root = Path(__file__).parents[1] / "src/agent_comms"
    source = "\n".join(
        (root / name).read_text()
        for name in (
            "input_disposition.py",
            "input_attempt.py",
            "goal_actions.py",
            "goal_management.py",
            "goal_waits.py",
            "owned_turn.py",
        )
    )
    for retired in (
        "AcpDeliveryCursors", "DeliveryCursor", "DeliveryDocument", "legacy_through",
        "GoalReplyScope", "direct_origins",
    ):
        assert retired not in source
    import ast

    tree = ast.parse((root / "goal_actions.py").read_text())
    retry = next(
        node
        for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == "RetryGoalAction"
    )
    assert not any(
        isinstance(node, ast.Attribute) and node.attr == "create_goal" for node in ast.walk(retry)
    )
