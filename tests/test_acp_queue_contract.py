"""Provider-free exact-ID ACP queue projection; no Pi consumption inference."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

import pytest
from acp import RequestError

from agent_comms.acp import CommsAgent
from agent_comms.acp_extension import (
    InputDeliveryChangedUpdate,
    InputStartedUpdate,
    QueueChangedUpdate,
    QueueItem,
    CursorScope,
    QueueScope,
    QueuePromptRequest,
    UnavailableQueueProjection,
    decode_updates,
    encode_request,
    encode_updates,
)
from agent_comms.child_process import ProcessIdentity
from agent_comms.comms import Comms
from agent_comms.queued_input import QueuedInput, QueuedInputContext
from agent_comms.runtime import present_session
from agent_comms.thread_identity import OwnerIdentity, ThreadIncarnation
from agent_comms.threads import Thread


def _owner(tmp_path: Path) -> tuple[Comms, CommsAgent, float, int]:
    comms = Comms(tmp_path / "wire")
    thread = Thread(
        "beta", frozenset(), str(tmp_path), process_identity=ProcessIdentity.capture(os.getpid())
    )
    comms.registry.declare(thread)
    agent = CommsAgent(comms)
    agent.sessions.bindings["beta"] = "beta"
    owner, admission_generation = comms.registry.live_owner_with_admission("beta")
    return comms, agent, owner.created_at, admission_generation


async def test_queue_exact_ids_restore_snapshot_and_admission_change(tmp_path, monkeypatch):
    comms, agent, created, generation = _owner(tmp_path)
    first, second = "a" * 32, "b" * 32
    agent.inputs.queued_inputs["beta"] = {
        key: QueuedInput(
            "same text",
            True,
            QueuedInputContext(OwnerIdentity(ThreadIncarnation("beta", created), generation)),
            key,
            "same text",
        )
        for key in (first, second)
    }
    initial = agent.inputs.queue_state("beta")
    assert initial.scope.admission_generation == generation
    assert initial.projection.items == (
        QueueItem(first, "same text"),
        QueueItem(second, "same text"),
    )
    updates = []

    async def record_update(*, session_id, update):
        assert session_id == "beta"
        updates.extend(decode_updates(update.field_meta))

    monkeypatch.setattr(agent._runtime, "session_update", record_update)
    item = agent.inputs.queued_inputs["beta"].pop(first)
    await agent.inputs.emit_input_started("beta", item.text, first, source_scope=initial.scope, native_id="b" * 32)
    started = updates[-1]
    assert isinstance(started, InputStartedUpdate)
    assert (started.input_id, started.scope, started.revision) == (
        first,
        initial.scope,
        initial.revision + 1,
    )
    await agent.inputs.emit_queue_state("beta")
    queued = updates[-1]
    assert queued.revision > started.revision
    assert queued.projection.items == (QueueItem(second, "same text"),)
    agent.inputs.restored_inputs["beta"] = {second: agent.inputs.queued_inputs["beta"].pop(second)}
    await agent.inputs.emit_queue_state("beta")
    restored = updates[-1]
    assert restored.projection.items == ()
    assert restored.projection.restored == (QueueItem(second, "same text"),)
    metadata = decode_updates(agent.sessions.metadata("beta", session_id="beta"))
    assert (
        next(f for f in metadata if isinstance(f, QueueChangedUpdate)).projection
        == restored.projection
    )
    comms.registry.register(comms.registry.require("beta"), new_owner=True)
    current = agent.inputs.queue_state("beta")
    assert current.scope.admission_generation > generation
    assert current.projection.items == current.projection.restored == ()
    assert second in agent.inputs.restored_inputs["beta"]


async def test_real_acp_surrogate_queue_ingress_stays_unknown_and_attachable(tmp_path):
    comms, agent, _, _ = _owner(tmp_path)
    comms.agents.begin_turn("beta", "active", "Held native fixture")
    inbox = agent.inputs.backend_inboxes["beta"] = asyncio.Queue()
    response = await agent.prompt(
        "beta",
        [{"type": "text", "text": "valid task"}],
        field_meta=encode_request(QueuePromptRequest("\ud800", True)),
    )
    exact = inbox.get_nowait()["_input_id"]
    assert any(
        isinstance(f, InputDeliveryChangedUpdate) for f in decode_updates(response.field_meta)
    )
    assert exact in agent.inputs.queued_inputs["beta"]
    assert agent.inputs.dispositions.read().rows["acp:" + exact].declared_name == "reserved"
    state = next(
        f
        for f in decode_updates(agent.sessions.metadata("beta", session_id="beta"))
        if isinstance(f, QueueChangedUpdate)
    )
    assert state.scope.owner.incarnation.name == "beta"
    assert isinstance(state.projection, UnavailableQueueProjection)
    assert exact in agent.inputs.queued_inputs["beta"]


@pytest.mark.parametrize("user_text", [["list"], [], {"text": "dict"}, 7, False])
async def test_real_acp_rejects_nonstring_user_text_before_unknown_or_enqueue(tmp_path, user_text):
    comms, agent, _, _ = _owner(tmp_path)
    comms.agents.begin_turn("beta", "active", "Held native fixture")
    inbox = agent.inputs.backend_inboxes["beta"] = asyncio.Queue()
    metadata = encode_request(QueuePromptRequest("valid", True))
    metadata["agentComms"]["request"]["user_text"] = user_text
    with pytest.raises(RequestError) as error:
        await agent.prompt("beta", [{"type": "text", "text": "valid task"}], field_meta=metadata)
    assert error.value.code == -32602
    assert inbox.empty()
    assert not agent.inputs.queued_inputs.get("beta")
    assert not agent.inputs.dispositions.path.exists()


@pytest.mark.parametrize(
    "rows",
    [
        {"malformed": ["not text"]},
        {str(i): "message" for i in range(33)},
        {"long": "x" * 4097},
    ],
)
async def test_unavailable_projection_never_drops_or_replays_owned_rows(tmp_path, rows):
    _, agent, created, generation = _owner(tmp_path)
    agent.inputs.queued_inputs["beta"] = {
        key: QueuedInput(
            text,
            True,
            QueuedInputContext(OwnerIdentity(ThreadIncarnation("beta", created), generation)),
            key,
            text,
        )
        for key, text in rows.items()
    }
    state = agent.inputs.queue_state("beta")
    assert state.scope is not None
    assert isinstance(state.projection, UnavailableQueueProjection)
    assert set(agent.inputs.queued_inputs["beta"]) == set(rows)


def test_alias_maps_only_attachment_session_id(tmp_path):
    comms, agent, _, _ = _owner(tmp_path)
    state = agent.inputs.queue_state("beta")
    (rebased,) = decode_updates(present_session(encode_updates(state), "alias"))
    assert rebased.scope.session_id == "alias"
    assert rebased.scope.owner == state.scope.owner
    assert rebased.projection is not None and rebased.projection == state.projection
    assert state.scope.session_id == "beta"


def test_attachment_routing_rename_preserves_exact_executor_fences():
    from dataclasses import replace
    original = OwnerIdentity(ThreadIncarnation("old-route", 1000.0), 7)
    renamed = replace(original, incarnation=replace(original.incarnation, name="new-route"))
    scopes = (QueueScope("official-acp-id", original, 123),
              CursorScope("official-acp-id", "a" * 32, original, 123))
    for scope in scopes:
        assert scope.relation(replace(scope, owner=renamed)).current
        assert scope.relation(replace(scope, session_id="new-route")).foreign
        assert scope.relation(replace(scope, owner_pid=124)).ambiguous
        assert scope.relation(replace(scope, owner=replace(original, incarnation=replace(original.incarnation, created_at=1001.0)))).ambiguous
        assert scope.relation(replace(scope, owner=replace(original, generation=8))).newer
        assert scope.relation(replace(scope, owner=replace(original, generation=6))).older
    assert scopes[1].relation(replace(scopes[1], wire_root_id="b" * 32)).foreign
    assert original != renamed  # Routing identity equality is deliberately unchanged.
