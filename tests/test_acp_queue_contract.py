"""Provider-free exact-ID ACP queue projection; no Pi consumption inference."""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from acp import RequestError
from acp.schema import TextContentBlock

from agent_comms.acp import CommsAgent
from agent_comms.acp_extension import (
    InputDeliveryChangedUpdate,
    IdentifiedInputStarted,
    UnidentifiedInputStarted,
    LiveCursorScope,
    LiveQueueScope,
    QueueChangedUpdate,
    QueuePromptRequest,
    SteerPromptRequest,
    UnavailableQueueProjection,
    decode_updates,
    encode_request,
    encode_updates,
)
from agent_comms.child_process import ProcessIdentity
from agent_comms.comms import Comms, wire
from agent_comms.errors import RelationViolationError
from agent_comms.field_codec import InputId, RootId, TurnId
from agent_comms.input_key import AcpInputKey
from agent_comms.input_attempt import ReservedInput
from agent_comms.queued_input import QueuedInput, QueuedInputContext
from agent_comms.runtime import present_session
from agent_comms.thread_identity import ClaimedIdentity, RecordedThreadIncarnation
from agent_comms.threads import Thread
from agent_comms.transcript_routes import NoDisplayText
from bus_record_fixture import prepare_native_comms
from stored_inputs import stored_inputs

def _owner(tmp_path: Path) -> tuple[Comms, CommsAgent, RecordedThreadIncarnation, int]:
    comms = prepare_native_comms(wire(tmp_path / "wire"))
    thread = Thread('beta', frozenset(), str(tmp_path), process_identity=ProcessIdentity.capture(os.getpid()))
    comms.registry.declare(thread)
    agent = CommsAgent(comms)
    agent.sessions.bindings["beta"] = "beta"
    owner, claimed_generation = comms.registry.live_owner_with_claim("beta")
    return comms, agent, owner.incarnation.require_recorded(), claimed_generation


async def test_real_acp_surrogate_queue_ingress_stays_unknown_and_attachable(tmp_path):
    comms, agent, _, _ = _owner(tmp_path)
    comms.agents.begin_turn(
        "beta", TurnId('96879611650f80a81392a52e0db9b023'), "Held native fixture"
    )
    inbox = agent.inputs.state('beta').open_inbox()
    response = await agent.prompt(
        "beta",
        [TextContentBlock(type="text", text="valid task")],
        field_meta=encode_request(QueuePromptRequest("\ud800", True)),
    )
    exact = inbox.get_nowait().input_id
    assert any(
        isinstance(f, InputDeliveryChangedUpdate) for f in decode_updates(response.field_meta)
    )
    assert exact in agent.inputs.state("beta").queued
    assert (type(stored_inputs(agent.inputs.dispositions.root).rows[AcpInputKey(exact)])
            is ReservedInput)
    state = next(
        f
        for f in decode_updates((await agent.sessions.metadata("beta", session_id="beta")))
        if isinstance(f, QueueChangedUpdate)
    )
    assert state.scope.claimed_identity.incarnation.name == "beta"
    assert isinstance(state.projection, UnavailableQueueProjection)
    assert exact in agent.inputs.state("beta").queued


async def test_request_identity_tracks_equal_queue_and_immediate_inputs_without_replay(tmp_path):
    comms, agent, _, _ = _owner(tmp_path)
    comms.agents.begin_turn(
        "beta", TurnId('96879611650f80a81392a52e0db9b023'), "Held native fixture"
    )
    inbox = agent.inputs.state('beta').open_inbox()
    requests = [QueuePromptRequest("same"), SteerPromptRequest("same")]
    for request in requests:
        response = await agent.prompt(
            "beta", [TextContentBlock(type="text", text="same")], field_meta=encode_request(request)
        )
        acknowledgement = next(f for f in decode_updates(response.field_meta) if isinstance(f, InputDeliveryChangedUpdate))
        assert acknowledgement == InputDeliveryChangedUpdate()
    assert requests[0].input_id != requests[1].input_id
    state = await agent.inputs.queue_state("beta")
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
    assert agent.inputs.input_keys("beta") == sources
    receipts = stored_inputs(agent.inputs.dispositions.root)
    cleared = receipts.transition(tuple(sources), lambda row: row.clear_unbound())
    assert all(row.public_status == "cleared" for row in cleared.rows.values())
    await agent.inputs.clear_queued_inputs("beta")
    assert not agent.inputs.state("beta").queued
    assert agent.inputs.input_keys("beta") == sources
    assert stored_inputs(agent.inputs.dispositions.root) == cleared
    await agent.inputs.input_refused("beta", requests[0].input_id)
    assert agent.inputs.input_keys("beta") == frozenset((AcpInputKey(requests[1].input_id),))
    assert stored_inputs(agent.inputs.dispositions.root) == cleared


@pytest.mark.parametrize("user_text", [["list"], [], {"text": "dict"}, 7, False])
async def test_real_acp_rejects_nonstring_user_text_before_unknown_or_enqueue(tmp_path, user_text):
    comms, agent, _, _ = _owner(tmp_path)
    comms.agents.begin_turn(
        "beta", TurnId('96879611650f80a81392a52e0db9b023'), "Held native fixture"
    )
    inbox = agent.inputs.state('beta').open_inbox()
    metadata = encode_request(QueuePromptRequest("valid", True))
    metadata["agentComms"]["request"]["user_text"] = user_text
    with pytest.raises(RequestError) as error:
        await agent.prompt(
            "beta", [TextContentBlock(type="text", text="valid task")], field_meta=metadata
        )
    assert error.value.code == -32602
    assert inbox.empty()
    assert not agent.inputs.state("beta").queued
    assert not stored_inputs(agent.inputs.dispositions.root).rows


@pytest.mark.parametrize(
    "rows",
    [
        {str(i): "message" for i in range(33)},
        {"long": "x" * 4097},
    ],
)
async def test_unavailable_projection_never_drops_or_replays_owned_rows(tmp_path, rows):
    _, agent, incarnation, generation = _owner(tmp_path)
    agent.inputs.state("beta").queued = {
        key: QueuedInput(
            text,
            True,
            QueuedInputContext(ClaimedIdentity(incarnation, generation)),
            key,
            text,
        )
        for key, text in rows.items()
    }
    state = await agent.inputs.queue_state("beta")
    assert state.scope.matches_session("beta")
    assert isinstance(state.projection, UnavailableQueueProjection)
    assert set(agent.inputs.state("beta").queued) == set(rows)


async def test_alias_maps_only_attachment_session_id(tmp_path):
    comms, agent, _, _ = _owner(tmp_path)
    state = await agent.inputs.queue_state("beta")
    (rebased,) = decode_updates(present_session(encode_updates(state), "alias"))
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
            session_id="official-acp-id", claimed_identity=original, owner=ProcessIdentity(123, 1),
        ),
        LiveCursorScope(
            session_id="official-acp-id", wire_root_id=RootId("a" * 32),
            claimed_identity=original, owner=ProcessIdentity(123, 1),
        ),
    )
    for scope in scopes:
        assert scope.relation(replace(scope, claimed_identity=renamed)).current
        assert scope.relation(replace(scope, session_id="new-route")).foreign
        assert scope.relation(replace(scope, owner=ProcessIdentity(124, 1))).ambiguous
        assert scope.relation(replace(scope, claimed_identity=replace(original, incarnation=replace(original.incarnation, created_at=1001.0)))).ambiguous
        assert scope.relation(replace(scope, claimed_identity=replace(original, claimed_generation=8))).newer
        assert scope.relation(replace(scope, claimed_identity=replace(original, claimed_generation=6))).older
    assert scopes[1].relation(replace(scopes[1], wire_root_id=RootId("b" * 32))).foreign
    assert original == renamed  # Birth owns identity; a name is only its route.
    assert original.incarnation.name != renamed.incarnation.name


async def test_ending_a_drained_session_leaves_no_state_for_it(tmp_path):
    comms, agent, _incarnation, _generation = _owner(tmp_path)
    await agent.inputs.queue_state("beta")
    agent.inputs.bind_native_turn("beta", comms.registry.require("beta"))
    agent.inputs.ensure_live_drain("beta")
    drained = agent.inputs.drained["beta"]
    drain = drained.drain_task
    assert drained.queue_revision == 1 and drained.turn_open() and drain is not None
    await agent.shutdown()
    assert "beta" not in agent.inputs.drained
    assert drain.done()


async def test_original_queued_and_routed_starts_own_their_input_identity(tmp_path):
    from agent_comms.channel_input_batch import SingleInputBatch
    from agent_comms.input_attempt import StartedInput
    from agent_comms.input_key import BusInputKey
    from agent_comms.text_digest import TextDigest
    from agent_comms.turn_goal_permission import InactiveGoalPermission, TurnGoalPermission
    from agent_comms.turn_input_source import (
        AcceptedTurn,
        NoInputDependency,
        NoTurnAcceptance,
        OwnerOriginalInput,
        RoutedOriginalInput,
    )

    _, agent, incarnation, generation = _owner(tmp_path)
    input_id, native_id = InputId.mint(), InputId.mint()
    row = StartedInput(
        AcpInputKey(input_id), None, "beta", generation, "beta", "original",
        turn_id=TurnId.mint(), native_id=native_id, sent_digest=TextDigest.of("original"),
    )
    scope = (await agent.inputs.queue_state("beta")).scope
    original = OwnerOriginalInput(
        acceptance=NoTurnAcceptance(),
        goal_permission=TurnGoalPermission(InactiveGoalPermission()),
        prompt="original", original_display=NoDisplayText(), origins=(), dependency=NoInputDependency(),
        batch=SingleInputBatch((row,)),
    )
    started = await original.acceptance.started_update(original, agent.inputs, "beta", row, scope)
    assert type(started) is IdentifiedInputStarted
    assert started.input_id == input_id and started.native_id == native_id

    queued_id = InputId.mint()
    agent.inputs.state("beta").queued[queued_id] = QueuedInput(
        "queued text", True, QueuedInputContext(ClaimedIdentity(incarnation, generation)),
        queued_id, "queued text",
    )
    accepted = await AcceptedTurn(queued_id).accepted_start(
        UnidentifiedInputStarted(None, scope, 1, native_id), agent.inputs, "beta",
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
        routed, agent.inputs, "beta", routed_row, scope)
    assert type(started) is UnidentifiedInputStarted
    assert not started.started_request(input_id)
