"""Provider-free exact-ID ACP queue projection; no Pi consumption inference."""

from __future__ import annotations

import asyncio
import os
from contextlib import ExitStack
from pathlib import Path

import pytest
from acp import RequestError

from agent_comms.acp import CommsAgent
from agent_comms.acp_extension import (
    InputDeliveryChangedUpdate,
    InputStartedUpdate,
    QueueChangedUpdate,
    QueueItem,
    LiveCursorScope,
    LiveQueueScope,
    QueuePromptRequest,
    SteerPromptRequest,
    UnavailableQueueProjection,
    decode_updates,
    encode_request,
    encode_updates,
)
from agent_comms.child_process import ProcessIdentity
from agent_comms.errors import RelationViolationError
from agent_comms.input_attempt import ReservedInput
from agent_comms.comms import Comms
from agent_comms.queued_input import QueuedInput, QueuedInputContext
from agent_comms.runtime import present_session
from agent_comms.thread_identity import ClaimedIdentity
from agent_comms.thread_identity import RecordedThreadIncarnation
from agent_comms.threads import Thread
from stored_inputs import stored_inputs


def _owner(tmp_path: Path) -> tuple[Comms, CommsAgent, float, int]:
    comms = Comms(tmp_path / "wire")
    thread = comms.registry.new_thread('beta', frozenset(), str(tmp_path), process_identity=ProcessIdentity.capture(os.getpid()))
    comms.registry.declare(thread)
    agent = CommsAgent(comms)
    agent.sessions.bindings["beta"] = "beta"
    owner, claimed_generation = comms.registry.live_owner_with_claim("beta")
    return comms, agent, owner.created_at, claimed_generation


async def test_real_acp_surrogate_queue_ingress_stays_unknown_and_attachable(tmp_path):
    comms, agent, _, _ = _owner(tmp_path)
    comms.agents.begin_turn("beta", "active", "Held native fixture")
    inbox = agent.inputs.state('beta').open_inbox()
    response = await agent.prompt(
        "beta",
        [{"type": "text", "text": "valid task"}],
        field_meta=encode_request(QueuePromptRequest("\ud800", True)),
    )
    exact = inbox.get_nowait().input_id.value
    assert any(
        isinstance(f, InputDeliveryChangedUpdate) for f in decode_updates(response.field_meta)
    )
    assert exact in agent.inputs.state("beta").queued
    assert stored_inputs(agent.inputs.dispositions.root).rows["acp:" + exact].declared_name == "reserved"
    state = next(
        f
        for f in decode_updates((await agent.sessions.metadata("beta", session_id="beta")))
        if isinstance(f, QueueChangedUpdate)
    )
    assert state.scope.claim_scope.incarnation.name == "beta"
    assert isinstance(state.projection, UnavailableQueueProjection)
    assert exact in agent.inputs.state("beta").queued


async def test_request_identity_tracks_equal_queue_and_immediate_inputs_without_replay(tmp_path):
    comms, agent, _, _ = _owner(tmp_path)
    comms.agents.begin_turn("beta", "active", "Held native fixture")
    inbox = agent.inputs.state('beta').open_inbox()
    requests = [QueuePromptRequest("same"), SteerPromptRequest("same")]
    for request in requests:
        response = await agent.prompt(
            "beta", [{"type": "text", "text": "same"}], field_meta=encode_request(request)
        )
        acknowledgement = next(f for f in decode_updates(response.field_meta) if isinstance(f, InputDeliveryChangedUpdate))
        assert acknowledgement.input_id == request.input_id
    assert requests[0].input_id != requests[1].input_id
    state = await agent.inputs.queue_state("beta")
    assert [item.input_id for item in state.projection.items] == [request.input_id for request in requests]
    assert all(row.public_status == "unknown" for row in stored_inputs(agent.inputs.dispositions.root).rows.values())
    before = inbox.qsize()
    with pytest.raises(RelationViolationError, match="reservation already exists"):
        await agent.prompt(
            "beta", [{"type": "text", "text": "same"}], field_meta=encode_request(requests[0])
        )
    assert inbox.qsize() == before
    assert len(stored_inputs(agent.inputs.dispositions.root).rows) == 2
    sources = frozenset(f"acp:{request.input_id}" for request in requests)
    assert agent.inputs.input_keys("beta") == sources
    receipts = stored_inputs(agent.inputs.dispositions.root)
    cleared = receipts.transition(tuple(sources), lambda row: row.clear_unbound())
    assert all(row.public_status == "cleared" for row in cleared.rows.values())
    await agent.inputs.clear_queued_inputs("beta")
    assert not agent.inputs.state("beta").queued
    assert agent.inputs.input_keys("beta") == sources
    assert stored_inputs(agent.inputs.dispositions.root) == cleared
    await agent.inputs.input_refused("beta", requests[0].input_id)
    assert agent.inputs.input_keys("beta") == frozenset((f"acp:{requests[1].input_id}",))
    assert stored_inputs(agent.inputs.dispositions.root) == cleared


@pytest.mark.parametrize("user_text", [["list"], [], {"text": "dict"}, 7, False])
async def test_real_acp_rejects_nonstring_user_text_before_unknown_or_enqueue(tmp_path, user_text):
    comms, agent, _, _ = _owner(tmp_path)
    comms.agents.begin_turn("beta", "active", "Held native fixture")
    inbox = agent.inputs.state('beta').open_inbox()
    metadata = encode_request(QueuePromptRequest("valid", True))
    metadata["agentComms"]["request"]["user_text"] = user_text
    with pytest.raises(RequestError) as error:
        await agent.prompt("beta", [{"type": "text", "text": "valid task"}], field_meta=metadata)
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
    _, agent, created, generation = _owner(tmp_path)
    agent.inputs.state("beta").queued = {
        key: QueuedInput(
            text,
            True,
            QueuedInputContext(ClaimedIdentity(RecordedThreadIncarnation("beta", created), generation)),
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
    assert rebased.scope.claim_scope == state.scope.claim_scope
    assert rebased.projection is not None and rebased.projection == state.projection
    assert state.scope.session_id == "beta"


def test_attachment_routing_rename_preserves_exact_executor_fences():
    from dataclasses import replace
    original = ClaimedIdentity(RecordedThreadIncarnation("old-route", 1000.0), 7)
    renamed = replace(original, incarnation=replace(original.incarnation, name="new-route"))
    scopes = (LiveQueueScope("official-acp-id", original, 123),
              LiveCursorScope("official-acp-id", "a" * 32, original, 123))
    for scope in scopes:
        assert scope.relation(replace(scope, claim_scope=renamed)).current
        assert scope.relation(replace(scope, session_id="new-route")).foreign
        assert scope.relation(replace(scope, owner_pid=124)).ambiguous
        assert scope.relation(replace(scope, claim_scope=replace(original, incarnation=replace(original.incarnation, created_at=1001.0)))).ambiguous
        assert scope.relation(replace(scope, claim_scope=replace(original, claimed_generation=8))).newer
        assert scope.relation(replace(scope, claim_scope=replace(original, claimed_generation=6))).older
    assert scopes[1].relation(replace(scopes[1], wire_root_id="b" * 32)).foreign
    assert original != renamed  # Routing identity equality is deliberately unchanged.


async def test_ending_a_drained_session_leaves_no_state_for_it(tmp_path):
    comms, agent, _created, _generation = _owner(tmp_path)
    await agent.inputs.queue_state("beta")
    agent.inputs.bind_native_turn("beta", comms.registry.require("beta"))
    agent.inputs.ensure_live_drain("beta")
    drained = agent.inputs.drained["beta"]
    drain = drained.drain_task
    assert drained.queue_revision == 1 and drained.turn_open() and drain is not None
    await agent.shutdown()
    assert "beta" not in agent.inputs.drained
    assert drain.done()
