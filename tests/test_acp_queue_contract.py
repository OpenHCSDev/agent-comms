"""Provider-free exact-ID ACP queue projection; no Pi consumption inference."""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from acp import RequestError
from acp.schema import TextContentBlock

from agent_comms.execution.acp import CommsAgent
from agent_comms.acp_extension import (
    IdentifiedInputStarted,
    InputDeliveryChangedUpdate,
    LiveQueueScope,
    QueueChangedUpdate,
    QueuePromptRequest,
    SteerPromptRequest,
    UnavailableQueueProjection,
    UnidentifiedInputStarted,
    decode_updates,
    encode_request,
    encode_updates,
)
from agent_comms.comms import Comms, wire
from agent_comms.errors import RelationViolationError
from agent_comms.execution.child_process import ProcessIdentity
from agent_comms.execution.input_key import AcpInputKey
from agent_comms.execution.queued_input import QueuedInput, QueuedInputContext
from agent_comms.execution.runtime import present_session
from agent_comms.execution.transcript_routes import NoDisplayText
from agent_comms.input_attempt import ReservedInput
from agent_comms.nominal.field_codec import AcpSessionId, InputId, RootId, TurnId
from agent_comms.projects.checkouts import Checkout
from agent_comms.registry.thread_identity import (
    ClaimedIdentity,
    RecordedThreadIncarnation,
)
from agent_comms.registry.threads import NativeThread
from agent_comms.transport.attachment_scope import LiveCursorScope
from bus_record_fixture import prepare_native_comms
from stored_inputs import stored_inputs

BETA = AcpSessionId("beta")

def _owner(tmp_path: Path) -> tuple[Comms, CommsAgent, RecordedThreadIncarnation, int]:
    comms = prepare_native_comms(wire(tmp_path / "wire"))
    thread = NativeThread('beta', frozenset(), Checkout(tmp_path), process_identity=ProcessIdentity.capture(os.getpid()))
    comms.registry.declare(thread)
    agent = CommsAgent(comms, auto_wake=False)
    agent.sessions.publish(AcpSessionId("beta"), thread)
    owner, claimed_generation = comms.registry.live_owner_with_claim("beta")
    return comms, agent, owner.incarnation.require_recorded(), claimed_generation


def held_owner(tmp_path: Path):
    """The owner's agent in a held native turn, with the inbox its inputs reach."""
    comms, agent, _, _ = _owner(tmp_path)
    comms.agents.begin_turn("beta", TurnId.mint(), "Held native fixture")
    return agent, agent.inputs.state(BETA).open_inbox()


async def test_real_acp_surrogate_queue_ingress_stays_unknown_and_attachable(tmp_path):
    agent, inbox = held_owner(tmp_path)
    response = await agent.prompt(
        "beta",
        [TextContentBlock(type="text", text="valid task")],
        field_meta=encode_request(QueuePromptRequest("\ud800", True)),
    )
    exact = inbox.get_nowait().input_id
    assert any(
        isinstance(f, InputDeliveryChangedUpdate) for f in decode_updates(response.field_meta)
    )
    assert exact in agent.inputs.state(BETA).queued
    assert (type(stored_inputs(agent.inputs.dispositions.root).rows[AcpInputKey(exact)])
            is ReservedInput)
    state = next(
        f
        for f in decode_updates((await agent.sessions.metadata("beta", session_id=AcpSessionId("beta"))))
        if isinstance(f, QueueChangedUpdate)
    )
    assert state.scope.claimed_identity.incarnation.name == "beta"
    assert isinstance(state.projection, UnavailableQueueProjection)
    assert exact in agent.inputs.state(BETA).queued


async def test_request_identity_tracks_equal_queue_and_immediate_inputs_without_replay(tmp_path):
    agent, inbox = held_owner(tmp_path)
    requests = [QueuePromptRequest("same"), SteerPromptRequest("same")]
    for request in requests:
        response = await agent.prompt(
            "beta", [TextContentBlock(type="text", text="same")], field_meta=encode_request(request)
        )
        acknowledgement = next(f for f in decode_updates(response.field_meta) if isinstance(f, InputDeliveryChangedUpdate))
        assert acknowledgement == InputDeliveryChangedUpdate()
    assert requests[0].input_id != requests[1].input_id
    state = await agent.inputs.queue_state(BETA)
    assert [item.input_id for item in state.projection.items] == [request.input_id for request in requests]
    assert all(row.public_status == "unknown" for row in stored_inputs(agent.inputs.dispositions.root).rows.values())
    before = inbox.qsize()
    with pytest.raises(RelationViolationError):
        await agent.prompt(
            "beta",
            [TextContentBlock(type="text", text="same")],
            field_meta=encode_request(requests[0]),
        )
    assert inbox.qsize() == before
    assert len(stored_inputs(agent.inputs.dispositions.root).rows) == 2
    sources = frozenset(AcpInputKey(request.input_id) for request in requests)
    assert agent.inputs.input_keys(BETA) == sources
    receipts = stored_inputs(agent.inputs.dispositions.root)
    cleared = receipts.transition(tuple(sources), lambda row: row.clear_unbound())
    assert all(row.public_status == "cleared" for row in cleared.rows.values())
    await agent.inputs.clear_queued_inputs(BETA)
    assert not agent.inputs.state(BETA).queued
    assert agent.inputs.input_keys(BETA) == sources
    assert stored_inputs(agent.inputs.dispositions.root) == cleared
    await agent.inputs.input_refused(BETA, requests[0].input_id)
    assert agent.inputs.input_keys(BETA) == frozenset((AcpInputKey(requests[1].input_id),))
    assert stored_inputs(agent.inputs.dispositions.root) == cleared


@pytest.mark.parametrize("user_text", [["list"], [], {"text": "dict"}, 7, False])
async def test_real_acp_rejects_nonstring_user_text_before_unknown_or_enqueue(tmp_path, user_text):
    agent, inbox = held_owner(tmp_path)
    metadata = encode_request(QueuePromptRequest("valid", True))
    metadata["agentComms"]["request"]["user_text"] = user_text
    with pytest.raises(RequestError) as error:
        await agent.prompt(
            "beta", [TextContentBlock(type="text", text="valid task")], field_meta=metadata
        )
    assert error.value.code == -32602
    assert inbox.empty()
    assert not agent.inputs.state(BETA).queued
    assert not stored_inputs(agent.inputs.dispositions.root).rows


@pytest.mark.parametrize("count,text", [
    (1, "日本語 \n" * 900),
    (32, "same text"),
    (17, "x" * 4096),
], ids=["long-unicode", "many-inputs", "large-total"])
async def test_queue_preview_preserves_long_input_identity_body_and_actions(tmp_path, count, text):
    from agent_comms.acp_extension import AvailableQueueProjection, QueueItem
    from agent_comms.execution.turn_inbox import (
        ClearQueuedInputs,
        SendNow,
    )

    agent, inbox = held_owner(tmp_path)
    requests = [QueuePromptRequest(text) for _ in range(count)]
    for request in requests:
        await agent.prompt("beta", [TextContentBlock(type="text", text=text)],
                           field_meta=encode_request(request))
        native = inbox.get_nowait()
        assert native.input_id == request.input_id
        assert native.content.text == "User follow-up:\n" + text
    state = await agent.inputs.queue_state(BETA)
    (received,) = decode_updates(encode_updates(state))
    assert isinstance(received.projection, AvailableQueueProjection)
    assert [item.input_id for item in received.projection.items] == [r.input_id for r in requests]
    assert all(item.preview == QueueItem.summarize(text) for item in received.projection.items)
    assert all(item.text == text for item in agent.inputs.state(BETA).queued.values())
    await agent.inputs.send_now(BETA)
    assert inbox.get_nowait() == SendNow(tuple(r.input_id for r in requests))
    await agent.inputs.clear_queued_inputs(BETA)
    assert inbox.get_nowait() == ClearQueuedInputs(tuple(r.input_id for r in requests))
    assert not agent.inputs.state(BETA).queued
    assert (await agent.inputs.queue_state(BETA)).projection.status == "available"


async def test_alias_maps_only_attachment_session_id(tmp_path):
    comms, agent, _, _ = _owner(tmp_path)
    state = await agent.inputs.queue_state(BETA)
    (rebased,) = decode_updates(present_session(encode_updates(state), AcpSessionId("alias")))
    assert rebased.scope.session_id == "alias"
    assert rebased.scope.claimed_identity == state.scope.claimed_identity
    assert rebased.projection is not None and rebased.projection == state.projection
    assert state.scope.session_id == "beta"


def test_attachment_routing_rename_preserves_exact_executor_fences():
    from dataclasses import replace
    original = ClaimedIdentity(RecordedThreadIncarnation("old-route", 1000.0), 7)
    renamed = replace(original, incarnation=replace(original.incarnation, name="new-route"))
    scopes = (
        LiveQueueScope(
            session_id=AcpSessionId("official-acp-id"), claimed_identity=original, owner=ProcessIdentity(123, 1),
        ),
        LiveCursorScope(
            session_id=AcpSessionId("official-acp-id"), wire_root_id=RootId("a" * 32),
            claimed_identity=original, owner=ProcessIdentity(123, 1),
        ),
    )
    for scope in scopes:
        assert scope.relation(replace(scope, claimed_identity=renamed)).current
        assert scope.relation(replace(scope, session_id=AcpSessionId("new-route"))).foreign
        assert scope.relation(replace(scope, owner=ProcessIdentity(124, 1))).ambiguous
        assert scope.relation(replace(scope, claimed_identity=replace(original, incarnation=replace(original.incarnation, created_at=1001.0)))).ambiguous
        assert scope.relation(replace(scope, claimed_identity=replace(original, claimed_generation=8))).newer
        assert scope.relation(replace(scope, claimed_identity=replace(original, claimed_generation=6))).older
    assert scopes[1].relation(replace(scopes[1], wire_root_id=RootId("b" * 32))).foreign
    assert original == renamed  # Birth owns identity; a name is only its route.
    assert original.incarnation.name != renamed.incarnation.name


async def test_ending_a_drained_session_leaves_no_state_for_it(tmp_path):
    comms, agent, _incarnation, _generation = _owner(tmp_path)
    await agent.inputs.queue_state(BETA)
    agent.inputs.bind_native_turn(BETA, comms.registry.require("beta"))
    agent.inputs.ensure_live_drain(BETA)
    drained = agent.inputs.drained[BETA]
    drain = drained.observer
    assert drained.queue_revision == 1 and drained.turn_open() and drain.busy
    await agent.shutdown()
    assert BETA not in agent.inputs.drained
    assert not drain.busy


async def test_original_queued_and_routed_starts_own_their_input_identity(tmp_path):
    from agent_comms.channel_input_batch import SingleInputBatch
    from agent_comms.execution.input_key import BusInputKey
    from agent_comms.execution.queued_input import AcceptedTurn
    from agent_comms.execution.turn_goal_permission import (
        InactiveGoalPermission,
        TurnGoalPermission,
    )
    from agent_comms.execution.turn_input_source import (
        NoInputDependency,
        NoTurnAcceptance,
        OwnerOriginalInput,
        RoutedOriginalInput,
    )
    from agent_comms.input_attempt import StartedInput
    from agent_comms.nominal.text_digest import TextDigest

    _, agent, incarnation, generation = _owner(tmp_path)
    input_id, native_id = InputId.mint(), InputId.mint()
    row = StartedInput(
        AcpInputKey(input_id), None, "beta", generation, "beta", "original",
        turn_id=TurnId.mint(), native_id=native_id, sent_digest=TextDigest.of("original"),
    )
    scope = (await agent.inputs.queue_state(BETA)).scope
    original = OwnerOriginalInput(
        acceptance=NoTurnAcceptance(),
        goal_permission=TurnGoalPermission(InactiveGoalPermission()),
        prompt="original", original_display=NoDisplayText(), origins=(), dependency=NoInputDependency(),
        batch=SingleInputBatch((row,)),
    )
    started = await original.acceptance.started_update(original, agent.inputs, AcpSessionId("beta"), row, scope)
    assert type(started) is IdentifiedInputStarted
    assert started.input_id == input_id and started.native_id == native_id

    queued_id = InputId.mint()
    agent.inputs.state(BETA).queued[queued_id] = QueuedInput(
        "queued text", True, QueuedInputContext(ClaimedIdentity(incarnation, generation)),
        queued_id, "queued text",
    )
    accepted = await AcceptedTurn(queued_id).accepted_start(
        UnidentifiedInputStarted(None, scope, 1, native_id), agent.inputs, AcpSessionId("beta"),
    )
    assert type(accepted) is IdentifiedInputStarted
    assert accepted.input_id == queued_id and accepted.text == "queued text"

    routed_row = StartedInput(
        BusInputKey(7), 7, "beta", generation, "beta", "routed",
        turn_id=TurnId.mint(), native_id=native_id, sent_digest=TextDigest.of("routed"),
    )
    routed = RoutedOriginalInput(
        acceptance=NoTurnAcceptance(), goal_permission=original.goal_permission,
        prompt="routed", original_display=NoDisplayText(), origins=(), dependency=NoInputDependency(),
        batch=SingleInputBatch((routed_row,)),
    )
    started = await routed.acceptance.started_update(
        routed, agent.inputs, AcpSessionId("beta"), routed_row, scope)
    assert type(started) is UnidentifiedInputStarted
    assert not started.started_request(input_id)
