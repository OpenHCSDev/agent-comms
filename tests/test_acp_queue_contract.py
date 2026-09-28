"""Provider-free exact-ID ACP queue projection; no Pi consumption inference."""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

import pytest
from acp import RequestError

from agent_comms.acp import CommsAgent
from agent_comms.child_process import ProcessIdentity
from agent_comms.comms import Comms
from agent_comms.input_drain import QueuedInput
from agent_comms.runtime import _present_cursor_session
from agent_comms.threads import Thread


def _owner(tmp_path: Path) -> tuple[Comms, CommsAgent, float, int]:
    comms = Comms(tmp_path / "wire")
    thread = Thread(
        "beta", frozenset(), str(tmp_path), process_identity=ProcessIdentity.capture(os.getpid())
    )
    comms.threads.register(thread)
    agent = CommsAgent(comms)
    agent.sessions.bindings["beta"] = "beta"
    owner, admission_generation = comms.registry.live_owner_with_admission("beta")
    return comms, agent, owner.created_at, admission_generation


async def test_queue_exact_ids_restore_snapshot_and_admission_change(tmp_path, monkeypatch):
    comms, agent, created, admission_generation = _owner(tmp_path)
    first = "a" * 32
    second = "b" * 32
    agent.inputs.queued_inputs["beta"] = {
        first: QueuedInput("same text", True, created, admission_generation),
        second: QueuedInput("same text", True, created, admission_generation),
    }
    binding, initial = agent.inputs.queue_state("beta")
    assert binding is not None and initial is not None
    assert binding["admissionGeneration"] == binding["ownerEpoch"] == admission_generation
    assert initial["scope"] == {key: value for key, value in binding.items() if key != "version"}
    assert initial["items"] == [
        {"inputId": first, "text": "same text"},
        {"inputId": second, "text": "same text"},
    ]
    assert initial["restored"] == []
    updates = []

    async def record_update(*, session_id, update):
        assert session_id == "beta"
        updates.append(update.field_meta["agentComms"])

    monkeypatch.setattr(agent._runtime, "session_update", record_update)
    item = agent.inputs.queued_inputs["beta"].pop(first)
    await agent.inputs.emit_input_started("beta", item.text, first, queued_item=item)
    started = updates[-1]["inputStarted"]
    assert (started["inputId"], started["scope"], started["revision"]) == (
        first,
        initial["scope"],
        initial["revision"] + 1,
    )
    await agent.inputs.emit_queue_state("beta")
    assert set(updates[-1]) == {"queueBinding", "queueState"}
    queued = updates[-1]["queueState"]
    assert queued["revision"] > started["revision"]
    assert queued["items"] == [{"inputId": second, "text": "same text"}]
    agent.inputs.restored_inputs["beta"] = {second: agent.inputs.queued_inputs["beta"].pop(second)}
    await agent.inputs.emit_queue_state("beta")
    restored = updates[-1]["queueState"]
    assert restored["items"] == []
    assert restored["restored"] == [{"inputId": second, "text": "same text"}]
    assert (
        agent.sessions.metadata("beta", session_id="beta")["agentComms"]["queueState"]["restored"]
        == restored["restored"]
    )

    # Owner admission changes without consuming or deleting old exact-ID rows.
    owner = comms.registry.require("beta")
    comms.registry.register(owner, new_owner=True)
    new_binding, new_state = agent.inputs.queue_state("beta")
    assert new_binding is not None and new_state is not None
    assert new_binding["ownerEpoch"] > admission_generation
    assert new_state["items"] == new_state["restored"] == []
    assert second in agent.inputs.restored_inputs["beta"]


async def test_real_acp_surrogate_queue_ingress_stays_unknown_and_attachable(tmp_path):
    _, agent, _, _ = _owner(tmp_path)
    agent.turns.active_turns["beta"] = "fake-active"
    inbox = agent.inputs.backend_inboxes["beta"] = asyncio.Queue()
    response = await agent.prompt(
        "beta",
        [{"type": "text", "text": "valid model task"}],
        agentComms={"delivery": "queue", "deferDisplay": True, "userText": "\ud800"},
    )
    exact = inbox.get_nowait()["_input_id"]
    assert response.field_meta["agentComms"]["inputDisposition"]["inputId"] == exact
    assert exact in agent.inputs.queued_inputs["beta"]
    assert agent.inputs.dispositions.read().rows.get("acp:" + exact).declared_name == "reserved"
    meta = agent.sessions.metadata("beta", session_id="beta")["agentComms"]
    assert meta["queueBinding"]["ownerThread"] == "beta"
    assert meta["queueState"] is None
    assert exact in agent.inputs.queued_inputs["beta"]  # no drop, skip, or replay


@pytest.mark.parametrize("user_text", [["list"], [], {"text": "dict"}, 7, False, None])
async def test_real_acp_rejects_nonstring_user_text_before_unknown_or_enqueue(tmp_path, user_text):
    _, agent, _, _ = _owner(tmp_path)
    agent.turns.active_turns["beta"] = "fake-active"
    inbox = agent.inputs.backend_inboxes["beta"] = asyncio.Queue()
    with pytest.raises(RequestError) as error:
        await agent.prompt(
            "beta",
            [{"type": "text", "text": "valid model task"}],
            agentComms={"delivery": "queue", "deferDisplay": True, "userText": user_text},
        )
    assert error.value.code == -32602
    assert error.value.data == {"reason": "userText must be a string"}
    assert inbox.empty()
    assert agent.inputs.queued_inputs.get("beta", {}) == {}
    assert agent.inputs.forwarded_inputs.get("beta", set()) == set()
    assert not agent.inputs.dispositions.path.exists()
    assert (
        agent.sessions.metadata("beta", session_id="beta")["agentComms"]["queueState"]["items"]
        == []
    )


async def test_legacy_malformed_queue_text_is_unavailable_without_dropping_id(tmp_path):
    _, agent, created, admission_generation = _owner(tmp_path)
    exact = "e" * 32
    agent.inputs.queued_inputs["beta"] = {
        exact: QueuedInput(["malformed"], True, created, admission_generation)
    }
    binding, state = agent.inputs.queue_state("beta")
    assert binding is not None and state is None
    meta = agent.sessions.metadata("beta", session_id="beta")["agentComms"]
    assert meta["queueBinding"] == binding and meta["queueState"] is None
    assert exact in agent.inputs.queued_inputs["beta"]


async def test_queue_overflow_unavailable_without_dropping_ids(tmp_path):
    _, agent, created, admission_generation = _owner(tmp_path)
    agent.inputs.queued_inputs["beta"] = {
        str(index): QueuedInput("message", True, created, admission_generation)
        for index in range(33)
    }
    binding, state = agent.inputs.queue_state("beta")
    assert binding is not None and state is None
    assert len(agent.inputs.queued_inputs["beta"]) == 33
    agent.inputs.queued_inputs["beta"] = {
        "long": QueuedInput("x" * 4097, True, created, admission_generation)
    }
    assert agent.inputs.queue_state("beta")[1] is None
    assert agent.inputs.queued_inputs["beta"]["long"].text == "x" * 4097


def test_queue_v1_fixture_orders_exact_ids_and_owner_scope():
    fixture = json.loads(
        (Path(__file__).parent / "fixtures" / "acp_exact_id_queue_v1.json").read_text()
    )
    bound = fixture["trustedLoad"]["queueBinding"]
    current = fixture["trustedLoad"]["queueState"]
    assert current["scope"] == {key: value for key, value in bound.items() if key != "version"}
    assert len({row["inputId"] for row in current["items"]}) == 2
    for event in fixture["updates"]:
        value = event["value"]
        assert value["scope"] == current["scope"]
        if value["revision"] <= current["revision"]:
            assert event["decision"] == "reject_stale_revision"
            continue
        if event["kind"] == "inputStarted":
            assert event["decision"] == "accept_exact_id"
            current = {
                **current,
                "revision": value["revision"],
                "items": [row for row in current["items"] if row["inputId"] != value["inputId"]],
            }
        else:
            assert event["decision"] == "accept"
            current = value
    assert current["revision"] == fixture["expected"]["revision"]
    assert current["items"] == fixture["expected"]["items"]
    assert [row["inputId"] for row in current["restored"]] == (
        fixture["expected"]["restoredInputIds"]
    )
    next_binding = fixture["nextTrustedLoad"]["queueBinding"]
    next_state = fixture["nextTrustedLoad"]["queueState"]
    assert next_binding["ownerEpoch"] > bound["ownerEpoch"]
    assert next_state["items"] == next_state["restored"] == []
    assert fixture["lateOldUpdate"]["value"]["scope"] != next_state["scope"]
    assert fixture["lateOldUpdate"]["decision"] == "reject_stale_scope"
    assert fixture["overflow"]["received"] > fixture["overflow"]["limit"]
    assert fixture["overflow"]["queueState"] is None
    assert fixture["autoReconnectReadyForwardedToClient"] is False
    race = fixture["prebindRace"]
    callback = race["callbackBeforeTrustedResult"]
    delayed = race["delayedTrustedLoad"]
    old_scope = delayed["queueState"]["scope"]
    assert all(
        callback["scope"][key] == old_scope[key]
        for key in ("sessionId", "ownerThread", "ownerCreatedAt")
    )
    assert callback["scope"]["ownerEpoch"] > old_scope["ownerEpoch"]
    assert race["expectedAfterDelayedLoad"] == {
        "status": "unavailable",
        "ownerEpochFloor": callback["scope"]["ownerEpoch"],
    }
    later = race["subsequentTrustedLoad"]
    assert later["queueBinding"]["ownerEpoch"] >= callback["scope"]["ownerEpoch"]
    assert later["queueState"]["revision"] > callback["revision"]
    assert later["queueState"]["items"] == []
    assert race["expectedAfterSubsequentLoad"]["status"] == "available"


def test_alias_proxy_maps_only_attachment_session_id():
    metadata = {
        "agentComms": {
            "queueBinding": {"version": 1, "sessionId": "beta", "ownerEpoch": 9},
            "queueState": {
                "version": 1,
                "scope": {"sessionId": "beta", "ownerEpoch": 9},
                "revision": 7,
                "items": [{"inputId": "id", "text": "body"}],
            },
            "inputStarted": {
                "version": 1,
                "scope": {"sessionId": "beta", "ownerEpoch": 9},
                "revision": 8,
                "inputId": "id",
            },
        }
    }
    _present_cursor_session(metadata, "alias")
    meta = metadata["agentComms"]
    assert meta["queueBinding"] == {"version": 1, "sessionId": "alias", "ownerEpoch": 9}
    assert meta["queueState"]["scope"] == {"sessionId": "alias", "ownerEpoch": 9}
    assert meta["inputStarted"]["scope"] == {"sessionId": "alias", "ownerEpoch": 9}
    assert meta["queueState"]["items"] == [{"inputId": "id", "text": "body"}]
