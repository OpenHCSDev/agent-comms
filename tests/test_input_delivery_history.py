"""Current owner queues scope notices without altering durable input evidence."""

from agent_comms.repo import Checkout
import asyncio
from dataclasses import replace
from pathlib import Path

import pytest
from acp.schema import TextContentBlock

from agent_comms.acp_extension import (
    GoalSnapshotReply, QueuePromptRequest, TurnChangedUpdate, UpdateMetadata, encode_request,
)
from agent_comms.child_process import NoProcess, ProcessIdentity
from agent_comms.errors import RelationViolationError
from agent_comms.input_key import AcpInputKey, BusInputKey
from agent_comms.comms import Comms, wire
from agent_comms.nominal.field_codec import FieldCodec, GoalId, InputId, TurnId
from agent_comms.input_attempt import (
    OwnerQueueInputDeliveryScope,
    UnobservedInputDeliveryScope,
    UnresolvedInput,
)
from agent_comms.input_disposition import InputDispositions
from agent_comms.goal_actions import SetGoalAction
from agent_comms.goals import NoGoal
from agent_comms.order_tools import InputDeliveryRead, OwnerGoalRead
from agent_comms.reported_turn import ReportedTurn
from agent_comms.thread_execution import ExternalThreadExecution, NativeThreadExecution, ThreadExecution
from agent_comms.thread_status import ConnectedThreadStatus, RunningThreadStatus, StoppedThreadStatus, ThreadStatus
from agent_comms.owner_connection import OwnerIdentityChangedError
from agent_comms.coordination_errors import StaleFence, StaleRevision
from agent_comms.threads import Thread
from agent_comms.tool_caller import ToolCaller
from agent_comms.ui_model.observation import ReadOwnerOperation, ReadRefused
from bus_record_fixture import prepare_native_comms
from delivery_owner_fixture import canonical_delivery_owner
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
    comms.registry.declare(Thread('owner', frozenset(), Checkout(tmp_path)))
    comms.registry.declare(Thread('peer', frozenset(), Checkout(tmp_path)))
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


async def test_mcp_and_the_ui_read_input_delivery_from_the_live_owner_queue(tmp_path):
    """2026-10-24: the commander hand-wrote SQL to list pi-mi's lost inputs; the tool then
    answered from the connector's own process, where no owner queue exists. Both boundaries
    now carry one InputDeliveryRead to the owner and receive the owner's own answer."""
    async with canonical_delivery_owner(tmp_path) as (comms, agent, _, _):
        comms.registry.register(comms.registry.require("alpha"), RunningThreadStatus())
        comms.agents.begin_turn("alpha", TurnId.mint(), "Held native fixture")
        agent.inputs.state("alpha").open_inbox()
        await agent.prompt("alpha", [TextContentBlock(type="text", text="queued")],
                           field_meta=encode_request(QueuePromptRequest("queued")))
        InputDispositions(comms.root).record(
            BusInputKey(91), seq=91, owner="alpha", claim_scope=1, target="alpha", text="Off queue")
        owner = comms.registry.require("alpha")
        scope = await agent.inputs.delivery_scope("alpha")
        assert type(scope) is OwnerQueueInputDeliveryScope
        unobserved = comms.goals.input_delivery("alpha")
        assert BusInputKey(91) in [row.input_id for row in unobserved.inputs]
        answers = {}
        for include_history in (False, True):
            owned = comms.goals.input_delivery("alpha", include_history=include_history, scope=scope)
            async with asyncio.timeout(30):
                mcp = await asyncio.to_thread(
                    InputDeliveryRead.invoke, comms,
                    {"thread": "alpha", "include_history": include_history},
                    caller=ToolCaller.from_actor("sender"))
                ui = await ReadOwnerOperation(
                    str(comms.root), owner.incarnation, InputDeliveryRead(include_history=include_history),
                ).answer(comms)
            assert mcp == FieldCodec.encode(owned)
            assert ui.value == owned
            answers[include_history] = owned
        assert BusInputKey(91) not in [row.input_id for row in answers[False].inputs]
        assert answers[False].historical_inputs == ()
        assert BusInputKey(91) in [row.input_id for row in answers[True].historical_inputs]
        assert replace(answers[True], historical_inputs=()) == answers[False]
        comms.registry.register(replace(owner, process_identity=NoProcess()), StoppedThreadStatus())
        original = stored_inputs(comms.root)
        original_bytes = InputDispositions(comms.root).path.read_bytes()
        for include_history in (False, True):
            retained = comms.goals.input_delivery("alpha", include_history=include_history)
            mcp = await asyncio.to_thread(
                InputDeliveryRead.invoke, comms,
                {"thread": "alpha", "include_history": include_history},
                caller=ToolCaller.from_actor("sender"))
            ui = await ReadOwnerOperation(
                str(comms.root), owner.incarnation, InputDeliveryRead(include_history=include_history),
            ).answer(comms)
            assert mcp == FieldCodec.encode(retained)
            assert ui.value == retained
            assert retained.current_scope is UnobservedInputDeliveryScope
            assert BusInputKey(91) in [row.input_id for row in retained.inputs]
        assert stored_inputs(comms.root) == original
        assert InputDispositions(comms.root).path.read_bytes() == original_bytes


async def test_mcp_and_the_ui_read_the_owner_goal_from_the_live_owner(tmp_path):
    """The owner answers comms_owner_goal under its own registration and session: MCP and the
    UI's owner envelope receive the owner's own projection, its turn metadata included, for an
    empty goal and for a set one."""
    async with canonical_delivery_owner(tmp_path) as (comms, agent, _, _):
        comms.registry.register(comms.registry.require("alpha"), RunningThreadStatus())
        comms.agents.begin_turn("alpha", TurnId.mint(), "Held native fixture")
        owner = comms.registry.require("alpha")
        replies = []
        for change in (lambda: None,
                       lambda: comms.goals.update_goal("alpha", SetGoalAction(text="Ship the read"))):
            change()
            owned = GoalSnapshotReply.from_snapshot(
                comms.goals.goal_snapshot("alpha"),
                UpdateMetadata.of(TurnChangedUpdate(agent.turns.turn_state("alpha"))))
            async with asyncio.timeout(30):
                mcp = await asyncio.to_thread(
                    OwnerGoalRead.invoke, comms, {"thread": "alpha"},
                    caller=ToolCaller.from_actor("sender"))
                ui = await ReadOwnerOperation(
                    str(comms.root), owner.incarnation, OwnerGoalRead()).answer(comms)
            assert mcp == FieldCodec.encode(owned)
            assert ui.value == owned
            assert ui.value.meta.batch.updates == (TurnChangedUpdate(agent.turns.turn_state("alpha")),)
            replies.append(owned)
        assert replies[0].goal == NoGoal()
        assert replies[1].goal.text == "Ship the read"
        comms.registry.register(replace(owner, process_identity=NoProcess()), StoppedThreadStatus())
        with pytest.raises(RelationViolationError):
            async with asyncio.timeout(30):
                await asyncio.to_thread(OwnerGoalRead.invoke, comms, {"thread": "alpha"},
                                        caller=ToolCaller.from_actor("sender"))


@pytest.mark.parametrize("execution,status", (
    (NativeThreadExecution, StoppedThreadStatus()),
    (ExternalThreadExecution, ConnectedThreadStatus()),
))
async def test_unavailable_native_queue_reads_retained_evidence_without_a_socket(
    tmp_path: Path, execution: type[ThreadExecution], status: ThreadStatus,
) -> None:
    comms: Comms = wire(tmp_path)
    thread = comms.registry.declare(Thread(
        'owner', frozenset(), Checkout(tmp_path), execution=execution,
        process_identity=ProcessIdentity.current(),
    ))
    comms.registry.register(thread, status)
    ledger = seed(comms, "owner")
    comms.goals.dismiss_historical_inputs(
        "owner", scope=OwnerQueueInputDeliveryScope(frozenset({BusInputKey(2), BusInputKey(8), UI_KEY})),
    )
    original = stored_inputs(comms.root)
    original_bytes = ledger.path.read_bytes()
    for include_history in (False, True):
        expected = comms.goals.input_delivery("owner", include_history=include_history)
        actual = await asyncio.to_thread(
            InputDeliveryRead.invoke, comms,
            {"thread": "owner", "include_history": include_history},
            caller=ToolCaller.from_actor("peer"),
        )
        ui = await ReadOwnerOperation(
            str(comms.root), thread.incarnation, InputDeliveryRead(include_history=include_history),
        ).answer(comms)
        assert actual == FieldCodec.encode(expected)
        assert ui.value == expected
        assert expected.current_scope is UnobservedInputDeliveryScope
        assert expected.dismissed_historical_count == 2
    assert await asyncio.to_thread(
        InputDeliveryRead.invoke, comms, {"thread": "owner"},
        caller=ToolCaller.from_actor("peer"),
    ) == FieldCodec.encode(comms.goals.input_delivery("owner", include_history=True))
    comms.goals.dismiss_historical_inputs("owner")
    assert stored_inputs(comms.root) == original
    assert ledger.path.read_bytes() == original_bytes


@pytest.mark.parametrize("recreate", (False, True))
async def test_retained_read_refuses_a_changed_recipient(tmp_path: Path, recreate: bool) -> None:
    comms: Comms = wire(tmp_path)
    owner = comms.registry.declare(Thread('owner', frozenset(), Checkout(tmp_path)))
    operation = InputDeliveryRead()
    cut = comms.registry.store.snapshot_with_authority()
    if recreate:
        comms.registry.unregister("owner")
        comms.registry.begin_delete("owner")
        with comms.registry.store.editing() as edit:
            edit.document.remove_originals((owner,))
            edit.commit()
        comms.registry.declare(Thread('owner', frozenset(), Checkout(tmp_path)))
    else:
        comms.registry.rename("owner", "renamed")
    with pytest.raises((OwnerIdentityChangedError, StaleFence)):
        await operation.answer(comms, owner.incarnation, cut)
    ui = await ReadOwnerOperation(str(comms.root), owner.incarnation, operation).answer(comms)
    assert isinstance(ui, ReadRefused)
    assert isinstance(ui.error, OwnerIdentityChangedError)


async def test_retained_read_refuses_replaced_root_and_foreign_ui_root(tmp_path: Path) -> None:
    root = tmp_path / "wire"
    comms: Comms = prepare_native_comms(wire(root))
    owner = comms.registry.declare(Thread('owner', frozenset(), Checkout(tmp_path)))
    operation = InputDeliveryRead()
    foreign = await ReadOwnerOperation(str(tmp_path / "foreign"), owner.incarnation, operation).answer(comms)
    assert isinstance(foreign, ReadRefused)
    assert isinstance(foreign.error, StaleRevision)
    cut = comms.registry.store.snapshot_with_authority()
    root.rename(tmp_path / "original")
    replacement = prepare_native_comms(wire(root))
    replacement.registry.declare(owner)
    with pytest.raises(StaleFence):
        await operation.answer(comms, owner.incarnation, cut)


def test_no_owner_queue_observation_never_dismisses_inputs(tmp_path):
    comms = wire(tmp_path)
    comms.registry.declare(Thread('owner', frozenset(), Checkout(tmp_path)))
    ledger = InputDispositions(tmp_path)
    ledger.record(BusInputKey(1), seq=1, owner="owner", claim_scope=1, target="owner", text="Hello")
    before = ledger.path.read_bytes()
    assert comms.goals.dismiss_historical_inputs("owner").historical_count == 0
    assert len(comms.goals.input_delivery("owner").inputs) == 1
    assert ledger.path.read_bytes() == before
    assert comms.goals.input_delivery("owner").current_scope is UnobservedInputDeliveryScope


