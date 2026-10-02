"""Goal admission uses each follow-up's authority, including goal origin turns.

The backend event source is controlled here to exercise exact admission races.
The callbacks, private goal ledger, ACP prompt queue, and transcript replay are real.
"""

import json
import logging

import pytest
from acp.schema import TextContentBlock

from agent_comms import agent_events as ae
from agent_comms.schedule_rules import WakeScheduleCheck
from agent_comms.acp_extension import (
    InputDeliveryChangedUpdate,
    QueuePromptRequest,
    TranscriptSnapshotUpdate,
    decode_updates,
    encode_request,
)
from agent_comms.comms import wire
from agent_comms.compaction_journal import CompactionJournal
from agent_comms.goal_actions import ClearGoalAction, SetGoalAction
from agent_comms.goal_generation import ReadyGeneration, ReservedGeneration
from agent_comms.input_disposition import InputDispositions
from agent_comms.transcript_events import ContextTranscript
from delivery_owner_fixture import canonical_agent


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
    agent.turns.adaptive_compaction_enabled = (
        False  # This case tests ordinary goal/input admission.
    )
    await agent.new_session(str(tmp_path / "project"))
    session = tmp_path / "session.jsonl"
    session.touch()
    comms.threads.attach_session("project", str(session))
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
    assert command["_input_id"] == public_id
    return public_id, command


async def replay(agent):
    updates = []

    class Client:
        transcript_snapshots = True

        async def session_update(self, session_id, update):
            updates.append(update)

    await agent.sessions.transcript.replay("project", "project", client=Client())
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
    caplog.set_level(logging.INFO, logger="agent_comms.owned_send_admission")
    agent, comms, session, _ = await owner(tmp_path, monkeypatch)
    observed = {}

    async def events(*args, **kwargs):
        with kwargs["send_boundary"](None, "a" * 32, args[2]) as allowed:
            assert allowed is True
        persist_user(session, "a" * 32, args[2])
        assert kwargs["native_start"](None, "a" * 32, args[2])
        yield ae.InputStarted(id=None)
        if queued_before_activation:
            public_id, command = await queue_followup(agent, kwargs)
        goal = comms.goals.update_goal("project", SetGoalAction(text="Read files until stopped"))
        yield ae.ToolEnd(id="set-goal", name="comms_set_goal", ok=True)
        assert agent.turns.goals.goal_store.snapshot(goal.id).lifecycle == ReservedGeneration()
        if not queued_before_activation:
            public_id, command = await queue_followup(agent, kwargs)
        observed.update(goal=goal, public_id=public_id)
        with kwargs["send_boundary"](public_id, "b" * 32, command["message"]) as allowed:
            assert allowed is (not queued_before_activation)
        if queued_before_activation:
            yield ae.InputRefused(id=public_id)
        else:
            persist_user(session, "b" * 32, command["message"])
            assert kwargs["native_start"](public_id, "b" * 32, command["message"])
            yield ae.InputStarted(id=public_id)
        yield ae.Chunk(text="Goal set and work completed this turn.")
        yield ae.StreamSettled()
        yield ae.Done(ok=True, text="Goal set and work completed this turn.")

    monkeypatch.setattr("agent_comms.backend.stream_agent_events", events)
    try:
        await agent.turns.run_agent_turn(
            "project", "project", "Set a goal for model", initial_display_text="Set a goal as typed"
        )
        assert comms.registry.require("project").goal.state.active
        state = agent.turns.goals.goal_store.snapshot(observed["goal"].id)
        assert state.lifecycle == ReadyGeneration() and state.number == 2
        key = "acp:" + observed["public_id"]
        row = InputDispositions(comms.root / InputDispositions.filename).read().lookup(key)
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
    caplog.set_level(logging.INFO, logger="agent_comms.owned_send_admission")
    agent, comms, session, _ = await owner(tmp_path, monkeypatch)
    store = agent.turns.goals.open_goal_store()
    original_goal = comms.goals.update_goal(
        "project", SetGoalAction(text="Read files until stopped"), owner_store=store
    )
    observed = {}

    async def events(*args, **kwargs):
        assert "Persistent goal" in args[2]
        public_id, command = await queue_followup(agent, kwargs)
        observed["public_id"] = public_id
        original = agent.inputs.original_sources["project"]
        assert original.notice_keys == ()  # Internal continuation is not a user echo.
        pending_keys = original.compaction_keys(str(session))
        assert len(pending_keys) == 1 and pending_keys[0].startswith("turn:")
        owner = comms.registry.require("project")
        future = agent.inputs.future_inputs(owner, pending_keys)
        assert tuple(future) == ("acp:" + public_id,)
        agent.inputs.dispositions.read().compaction_rows(owner, pending_keys, agent.inputs)
        with kwargs["send_boundary"](None, "a" * 32, args[2]) as allowed:
            assert allowed is True
        persist_user(session, "a" * 32, args[2])
        assert kwargs["native_start"](None, "a" * 32, args[2])
        yield ae.InputStarted(id=None)
        if change == "clear":
            comms.goals.update_goal("project", ClearGoalAction())
        elif change == "replace":
            observed["replacement"] = comms.goals.update_goal(
                "project", SetGoalAction(text="A different goal"), owner_store=store
            )
        with kwargs["send_boundary"](public_id, "b" * 32, command["message"]) as allowed:
            assert allowed is (change is None)
        if change is None:
            persist_user(session, "b" * 32, command["message"])
            assert kwargs["native_start"](public_id, "b" * 32, command["message"])
            yield ae.InputStarted(id=public_id)
        else:
            yield ae.InputRefused(id=public_id)
        yield ae.Chunk(text="Finished reading this section.")
        yield ae.StreamSettled()
        yield ae.Done(ok=change is None, text="Finished reading this section.")

    monkeypatch.setattr("agent_comms.backend.stream_agent_events", events)
    try:
        await agent.turns.run_agent_turn(
            "project", "project", "Continue working toward the active goal.", autonomous_goal=True
        )
        key = "acp:" + observed["public_id"]
        row = InputDispositions(comms.root / InputDispositions.filename).read().lookup(key)
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


@pytest.mark.asyncio
@pytest.mark.parametrize("activate_goal", [False, True])
async def test_unresolved_journal_reports_named_refusal_or_goal_deferral(
    tmp_path, monkeypatch, caplog, activate_goal
):
    caplog.set_level(logging.INFO, logger="agent_comms.owned_send_admission")
    agent, comms, session, _ = await owner(tmp_path, monkeypatch)
    observed = []

    async def events(*args, **kwargs):
        with kwargs["send_boundary"](None, "a" * 32, args[2]) as allowed:
            assert allowed is True
        persist_user(session, "a" * 32, args[2])
        assert kwargs["native_start"](None, "a" * 32, args[2])
        yield ae.InputStarted(id=None)
        if activate_goal:
            comms.goals.update_goal("project", SetGoalAction(text="Continue until stopped"))
        public_id, command = await queue_followup(agent, kwargs)
        journal = CompactionJournal(comms.root / "compaction-commits.sqlite3")
        commit_id = journal.operations.begin(
            str(session), {"source": "before-summary"}, inputs=agent.inputs.dispositions.read()
        )
        with kwargs["send_boundary"](public_id, "b" * 32, command["message"]) as allowed:
            observed.append(allowed)
        row = agent.inputs.dispositions.read().lookup("acp:" + public_id)
        assert not row.has_native_binding
        assert journal.operations.get(commit_id).state.declared_name == "intent"
        yield ae.StreamSettled()
        yield ae.Done(ok=True, text="No follow-up was sent")

    monkeypatch.setattr("agent_comms.backend.stream_agent_events", events)
    try:
        await agent.turns.run_agent_turn("project", "project", "Original owner input")
        assert observed == [None if activate_goal else False]
        assert "ordinary_journal:" in caplog.text
        assert ("deferred" if activate_goal else "refused") in caplog.text
    finally:
        await agent.shutdown()
