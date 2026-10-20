"""Goal claim_scope uses each follow-up's authority, including goal origin turns.

The backend event source is controlled here to exercise exact claim_scope races.
The callbacks, private goal ledger, ACP prompt queue, and transcript replay are real.
"""

import json
import logging
from contextlib import nullcontext

import pytest
from acp import RequestError
from acp.schema import TextContentBlock

import agent_comms.agent_events as ae
from agent_comms.schedule_rules import WakeScheduleCheck
from agent_comms.acp_extension import (
    InputDeliveryChangedUpdate,
    QueuePromptRequest,
    TranscriptSnapshotUpdate,
    decode_updates,
    encode_request,
)
from agent_comms.comms import wire
from agent_comms.goal_actions import ClearGoalAction, SetGoalAction
from agent_comms.goal_generation import ReadyGeneration, ReservedGeneration
from agent_comms.send_outcome import AuthorityChangedSend, GrantedSend
from agent_comms.owned_send import OwnedSend
from agent_comms.transcript_events import ContextTranscript
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


def persist_user(session, native_id, text):
    with session.open("a") as stream:
        stream.write(
            json.dumps(
                {
                    "type": "message",
                    "id": native_id[:8],
                    "message": {
                        "role": "user",
                        "inputId": native_id,
                        "content": [{"type": "text", "text": text}],
                    },
                }
            )
            + "\n"
        )


async def queue_followup(agent, kwargs):
    response = await agent.prompt(
        "project",
        [TextContentBlock(type="text", text="follow-up for model")],
        **encode_request(QueuePromptRequest(user_text="follow-up as typed", defer_display=True)),
    )
    public_id = next(
        update.input_id
        for update in decode_updates(response.field_meta)
        if isinstance(update, InputDeliveryChangedUpdate)
    )
    assert public_id is not None
    command = kwargs["steering_queue"].get_nowait()
    assert command.input_id.value == public_id
    return public_id, command


async def replay(agent):
    updates = []

    class Client:
        transcript_snapshots = True

        async def session_update(self, session_id, update):
            updates.append(update)

    await agent.sessions.transcript.replay("project", client=Client())
    page = next(
        update.page
        for update in decode_updates(updates[0].field_meta)
        if isinstance(update, TranscriptSnapshotUpdate)
    )
    return [item.text for item in page.events if not isinstance(item, ContextTranscript)]


@pytest.mark.asyncio
@pytest.mark.parametrize("queued_before_activation", [False, True])
async def test_origin_goal_allows_only_followup_admitted_after_activation(
    tmp_path, monkeypatch, queued_before_activation, caplog
):
    caplog.set_level(logging.INFO, logger=OwnedSend.__module__)
    agent, comms, session, _ = await owner(tmp_path, monkeypatch)
    observed = {}

    async def events(*args, **kwargs):
        with kwargs["send_boundary"](None, "a" * 32, args[2]) as allowed:
            assert isinstance(allowed, GrantedSend)
        persist_user(session, "a" * 32, args[2])
        assert kwargs["native_start"](None, "a" * 32, args[2])
        yield ae.OriginalInputStarted()
        if queued_before_activation:
            public_id, command = await queue_followup(agent, kwargs)
        goal = comms.goals.update_goal("project", SetGoalAction(text="Read files until stopped"))
        yield ae.ToolEnd(id="set-goal", name="comms_set_goal", ok=True)
        assert comms.goals.open_goal_store().snapshot(goal.id).lifecycle == ReservedGeneration()
        if not queued_before_activation:
            public_id, command = await queue_followup(agent, kwargs)
        observed.update(goal=goal, public_id=public_id)
        with kwargs["send_boundary"](public_id, "b" * 32, command.content.text) as allowed:
            assert isinstance(allowed, AuthorityChangedSend if queued_before_activation else GrantedSend)
        if queued_before_activation:
            yield ae.FollowingInputRefused(id=public_id)
        else:
            persist_user(session, "b" * 32, command.content.text)
            assert kwargs["native_start"](public_id, "b" * 32, command.content.text)
            yield ae.FollowingInputStarted(id=public_id)
        yield ae.Chunk(text="Goal set and work completed this turn.")
        yield ae.StreamSettled()
        yield ae.Done(ok=True, text="Goal set and work completed this turn.")

    monkeypatch.setattr("agent_comms.backend.stream_agent_events", events)
    try:
        await agent.turns.run_agent_turn(
            "project", "project", "Set a goal for model", initial_display_text="Set a goal as typed"
        )
        assert comms.registry.require("project").goal.state.active
        state = comms.goals.open_goal_store().snapshot(observed["goal"].id)
        assert state.lifecycle == ReadyGeneration() and state.number == 2
        key = "acp:" + observed["public_id"]
        row = stored_inputs(comms.root).lookup(key)
        assert row.declared_name == ("reserved" if queued_before_activation else "started")
        if queued_before_activation:
            assert not row.has_native_binding
            assert "accepted_input_authority:" in caplog.text
        assert await replay(agent) == ["Set a goal as typed"] + (
            [] if queued_before_activation else ["follow-up as typed"]
        )
    finally:
        await agent.shutdown()


@pytest.mark.asyncio
@pytest.mark.parametrize("change", [None, "clear", "replace"])
async def test_autonomous_goal_followup_checks_current_goal_and_hides_internal_prompt(
    tmp_path, monkeypatch, change, caplog
):
    caplog.set_level(logging.INFO, logger=OwnedSend.__module__)
    agent, comms, session, _ = await owner(tmp_path, monkeypatch)
    store = comms.goals.open_goal_store()
    original_goal = comms.goals.update_goal(
        "project", SetGoalAction(text="Read files until stopped")
    )
    observed = {}

    async def events(*args, **kwargs):
        assert "Persistent goal" in args[2]
        public_id, command = await queue_followup(agent, kwargs)
        observed["public_id"] = public_id
        original = agent.inputs.state("project").first_source
        assert original.notice_keys == ()  # Internal continuation is not a user echo.
        with kwargs["send_boundary"](None, "a" * 32, args[2]) as allowed:
            assert isinstance(allowed, GrantedSend)
        persist_user(session, "a" * 32, args[2])
        assert kwargs["native_start"](None, "a" * 32, args[2])
        yield ae.OriginalInputStarted()
        if change == "clear":
            comms.goals.update_goal("project", ClearGoalAction())
        elif change == "replace":
            observed["replacement"] = comms.goals.update_goal(
                "project", SetGoalAction(text="A different goal")
            )
        with kwargs["send_boundary"](public_id, "b" * 32, command.content.text) as allowed:
            assert isinstance(allowed, GrantedSend if change is None else AuthorityChangedSend)
        if change is None:
            persist_user(session, "b" * 32, command.content.text)
            assert kwargs["native_start"](public_id, "b" * 32, command.content.text)
            yield ae.FollowingInputStarted(id=public_id)
        else:
            yield ae.FollowingInputRefused(id=public_id)
        yield ae.Chunk(text="Finished reading this section.")
        yield ae.StreamSettled()
        yield ae.Done(ok=change is None, text="Finished reading this section.")

    monkeypatch.setattr("agent_comms.backend.stream_agent_events", events)
    try:
        with nullcontext() if change is None else pytest.raises(RequestError, match="Internal error"):
            await agent.turns.run_agent_turn(
                "project", "project", "Continue working toward the active goal.", autonomous_goal=True
            )
        key = "acp:" + observed["public_id"]
        row = stored_inputs(comms.root).lookup(key)
        assert row.declared_name == ("started" if change is None else "reserved")
        if change is not None:
            assert not row.has_native_binding
            assert "accepted_input_authority:" in caplog.text
        assert await replay(agent) == (["follow-up as typed"] if change is None else [])
        current = comms.registry.require("project").goal
        if change is None:
            assert current.state.active and current.id == original_goal.id
            assert store.snapshot(current.id).lifecycle == ReadyGeneration()
        elif change == "clear":
            assert current is None
        else:
            assert current.state.active and current.id == observed["replacement"].id
            assert store.snapshot(current.id).lifecycle == ReadyGeneration()
    finally:
        await agent.shutdown()
