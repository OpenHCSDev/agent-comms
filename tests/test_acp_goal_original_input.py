"""Original owner inputs use the goal authority captured at local claim_scope.

These tests control backend timing, exercising the real claim_scope callbacks,
private goal ledger, and durable UNKNOWN/STARTED transition without a provider.
"""

from agent_comms.queued_input import InitialInput
import json

import pytest
from agent_comms.acp_failure import NativeTurnError

import agent_comms.agent_events as ae
from agent_comms.schedule_rules import WakeScheduleCheck
from agent_comms.queued_input import InputHandoffRefused
from agent_comms.comms import wire
from agent_comms.goal_actions import SetGoalAction
from agent_comms.goal_generation import ReadyGeneration
from agent_comms.input_disposition import InputDispositions
from agent_comms.send_outcome import AuthorityChangedSend, GrantedSend
from delivery_owner_fixture import canonical_agent
from stored_inputs import stored_inputs


async def owner(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_COMMS_AGENT_MODELS", "test/model")
    comms = wire(tmp_path / "wire")
    agent = canonical_agent(comms, agent_bin="pi", runtime_enabled=True)
    monkeypatch.setattr(agent.inputs, "ensure_live_drain", lambda _session: None)
    monkeypatch.setattr(WakeScheduleCheck, "schedule", lambda _check: None)
    updates = []

    class Client:
        async def session_update(self, session_id, update):
            updates.append(update)

    agent.on_connect(Client())
    (tmp_path / "project").mkdir()
    await agent.new_session(str(tmp_path / "project"))
    session = tmp_path / "session.jsonl"
    session.touch()
    comms.threads.attach_session((attachment_owner := comms.registry.require("project")), str(session), process=attachment_owner.process_identity)
    return agent, comms, session, updates


def disposition_rows(agent):
    return list(stored_inputs(agent.inputs.dispositions.root).rows.values())


@pytest.mark.asyncio
async def test_idle_owner_original_input_preserves_autonomous_goal_grant(tmp_path, monkeypatch):
    agent, comms, session, _ = await owner(tmp_path, monkeypatch)
    store = comms.goals.open_goal_store()
    goal = comms.goals.update_goal("project", SetGoalAction(text="Keep reading"))
    prior_generation = store.snapshot(goal.id)

    async def events(*args, **kwargs):
        native_id = "a" * 32
        with kwargs["send_boundary"](None, native_id, args[2]) as allowed:
            assert isinstance(allowed, GrantedSend)
        session.write_text(
            json.dumps(
                {
                    "type": "message",
                    "id": "owner-input",
                    "message": {
                        "role": "user",
                        "inputId": native_id,
                        "content": [{"type": "text", "text": args[2]}],
                    },
                }
            )
            + "\n"
        )
        assert kwargs["native_start"](None, native_id, args[2])
        yield ae.OriginalInputStarted()
        yield ae.Chunk(text="Read the requested file.")
        yield ae.StreamSettled()
        yield ae.Done(ok=True, text="Read the requested file.")

    monkeypatch.setattr("agent_comms.backend.stream_agent_events", events)
    try:
        await InitialInput.run(agent.inputs, "project", "project", "testing steering")
        rows = disposition_rows(agent)
        assert len(rows) == 1 and rows[0].declared_name == "started"
        current = comms.registry.require("project").goal
        assert current.id == goal.id and current.state.active
        generation = store.snapshot(goal.id)
        # Fresh owner input has its own authority; it does not spend the autonomous grant.
        assert generation == prior_generation
    finally:
        await agent.shutdown()


@pytest.mark.asyncio
@pytest.mark.parametrize("existing_goal", [False, True], ids=["activated", "replaced"])
async def test_changed_goal_before_original_turn_does_not_consume_new_grant(
    tmp_path, monkeypatch, existing_goal
):
    agent, comms, _, _ = await owner(tmp_path, monkeypatch)
    store = comms.goals.open_goal_store()
    if existing_goal:
        comms.goals.update_goal("project", SetGoalAction(text="Original goal"))
    original_emit = agent.inputs.emit_input_disposition
    replacement = None
    backend_calls = 0

    async def activate_after_claim(session_id, row):
        nonlocal replacement
        await original_emit(session_id, row)
        if replacement is None:
            replacement = comms.goals.update_goal(
                "project", SetGoalAction(text="New authority")
            )

    async def events(*args, **kwargs):
        nonlocal backend_calls
        backend_calls += 1
        yield ae.Done(ok=False, text="Must not reach backend")

    monkeypatch.setattr(agent.inputs, "emit_input_disposition", activate_after_claim)
    monkeypatch.setattr("agent_comms.backend.stream_agent_events", events)
    try:
        with pytest.raises(InputHandoffRefused, match="Queued input acceptance context changed"):
            await InitialInput.run(agent.inputs, "project", "project", "authorized before goal change")
        assert backend_calls == 0
        rows = disposition_rows(agent)
        assert len(rows) == 1 and rows[0].unresolved
        assert not rows[0].has_native_binding
        current = comms.registry.require("project").goal
        assert current.id == replacement.id and current.state.active
        generation = store.snapshot(replacement.id)
        assert generation.lifecycle == ReadyGeneration() and generation.number == 1
        reopened = InputDispositions(comms.root)
        assert len(reopened.unresolved(frozenset({"project"})).unknown(frozenset({"project"}))) == 1
    finally:
        await agent.shutdown()


@pytest.mark.asyncio
async def test_original_goal_input_cannot_send_after_owner_stops(tmp_path, monkeypatch):
    agent, comms, _, _ = await owner(tmp_path, monkeypatch)
    store = comms.goals.open_goal_store()
    goal = comms.goals.update_goal("project", SetGoalAction(text="Keep reading"))
    prior_generation = store.snapshot(goal.id)
    boundaries = []

    async def events(*args, **kwargs):
        comms.registry.unregister("project")
        with kwargs["send_boundary"](None, "b" * 32, args[2]) as allowed:
            boundaries.append(type(allowed))
        yield ae.Done(ok=False, text="Owner stopped before send")

    monkeypatch.setattr("agent_comms.backend.stream_agent_events", events)
    try:
        with pytest.raises(NativeTurnError, match="Owner stopped before send"):
            await InitialInput.run(agent.inputs, "project", "project", "do not send after stop")
        assert boundaries == [AuthorityChangedSend]
        rows = disposition_rows(agent)
        assert len(rows) == 1 and rows[0].unresolved
        assert not rows[0].has_native_binding
        # A refused owner input cannot spend an unrelated autonomous grant.
        assert store.snapshot(goal.id) == prior_generation
    finally:
        await agent.shutdown()
