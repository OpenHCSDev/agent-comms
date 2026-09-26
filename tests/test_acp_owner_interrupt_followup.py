"""Fresh ACP owner follow-ups during a goal-parked DM are not goal retries."""

import asyncio
import json
import os
import sys
from dataclasses import replace

import pytest
from acp.schema import TextContentBlock

from test_goal_direct_interrupt import _owner


@pytest.mark.parametrize(
    "standby,change",
    [
        (False, None),
        (True, None),
        (True, "goal_revision"),
        (True, "goal_replaced"),
        (True, "owner_pause"),
        (True, "wait_cleared"),
        (True, "owner_replaced"),
        (True, "owner_stopped"),
    ],
)
async def test_fresh_owner_followup_during_direct_interrupt(tmp_path, monkeypatch, standby, change):
    comms, agent, session, goal = await _owner(tmp_path, monkeypatch, standby=standby)
    monkeypatch.setattr(agent, "_schedule_wake", lambda _session: None)
    before_goal = comms.registry.require(session).goal
    before_wait = comms.goal_wait(session)
    message = comms.send_message("outsider", session, "Question while goal is parked")
    assert await agent._drain_owned_inbox(session) == 1
    queued = agent._pending_turns.pop(session)[0]
    gate_facts = []
    expected_state = {"goal": before_goal, "wait": before_wait}
    admission = comms.registry.snapshot().admission_generations[session]
    old_key = "acp:historical-uncertain"
    agent._dispositions.record(
        old_key,
        seq=None,
        owner=session,
        admission=admission,
        target=session,
        text="Never replay historical uncertainty",
    )
    old_row = agent._dispositions.get(old_key)

    async def events(*args, **kwargs):
        native_id = "a" * 32
        with kwargs["send_boundary"](None, native_id, args[2]) as allowed:
            assert allowed is True
        assert kwargs["native_start"](None, native_id, args[2])
        yield {"type": "input_started", "id": None}
        # Both the public goal and original native input remain live; this is
        # not a terminal/cleared-turn race or a stale optional awareness frame.
        assert comms.registry.require(session).active_turn is not None
        response = await asyncio.create_task(
            agent.prompt(
                session,
                [TextContentBlock(type="text", text="Genuine fresh owner instruction")],
                agentComms={"deferDisplay": True},
            )
        )
        public_id = response.field_meta["agentComms"]["inputDisposition"]["inputId"]
        command = kwargs["steering_queue"].get_nowait()
        assert command["_input_id"] == public_id
        if change == "goal_revision":
            comms.update_goal(session, "active", goal_id=goal.id, progress="New revision")
        elif change == "goal_replaced":
            comms.update_goal(session, "set", text="New goal")
        elif change == "owner_pause":
            comms.update_goal(session, "paused", goal_id=goal.id, owner_action=True)
        elif change == "wait_cleared":
            assert comms.consume_goal_wait(session, before_wait.wait_id)
        elif change == "owner_replaced":
            current = comms.registry.require(session)
            comms.registry.register(
                replace(current, created_at=current.created_at + 1, active_turn=None)
            )
        elif change == "owner_stopped":
            comms.registry.unregister(session)
        expected_state.update(
            goal=comms.registry.require(session).goal, wait=comms.goal_wait(session)
        )
        boundary = kwargs["send_boundary"](public_id, "b" * 32, command["message"])
        with boundary as allowed:
            local = boundary.gen.gi_frame.f_locals
            gate_facts.append(
                {
                    key: local.get(key)
                    for key in ("owner_ok", "goal_ok", "owner_followup", "direct_interrupt")
                }
            )
            assert allowed is (change is None), gate_facts
        if change is None:
            assert kwargs["native_start"](public_id, "b" * 32, command["message"])
            yield {"type": "input_started", "id": public_id}
            # STARTED is not a reusable send right, even with unchanged owner.
            before_duplicate = agent._dispositions.path.read_bytes()
            with kwargs["send_boundary"](public_id, "c" * 32, command["message"]) as allowed:
                assert allowed is False
            assert agent._dispositions.path.read_bytes() == before_duplicate
        else:
            yield {"type": "input_refused", "id": public_id}
        yield {"type": "settled"}
        yield {"type": "done", "ok": change is None, "text": "No goal authority was used"}

    monkeypatch.setattr("agent_comms.acp.backend.stream_agent_events", events)
    try:
        await agent._run_agent_turn(
            session,
            session,
            queued.prompt,
            origins=(queued.origin,),
            direct_interrupt_goal_id=queued.direct_interrupt_goal_id,
            direct_interrupt_goal_revision=queued.direct_interrupt_goal_revision,
            direct_interrupt_wait_id=queued.direct_interrupt_wait_id,
            direct_interrupt_input_key=queued.direct_interrupt_input_key,
            direct_interrupt_ticket=queued.direct_interrupt_ticket,
        )
        assert agent._dispositions.status(f"bus:{message.seq}") == "started"
        owner_rows = [
            r
            for k, r in agent._dispositions._read().items()
            if k.startswith("acp:") and k != old_key
        ]
        assert len(owner_rows) == 1
        assert owner_rows[0]["status"] == ("started" if change is None else "unknown")
        if change is not None:
            assert owner_rows[0]["native_id"] is None
        assert agent._dispositions.get(old_key) == old_row
        assert comms.registry.require(session).goal == expected_state["goal"]
        assert comms.goal_wait(session) == expected_state["wait"]
        assert not (comms.root / "goal-private").exists()
        assert not agent._pending_turns.get(session)
    finally:
        await agent.shutdown()


@pytest.mark.skipif(os.name == "nt", reason="POSIX fake Pi executable")
@pytest.mark.parametrize("standby", [False, True])
async def test_real_fake_pi_receives_owner_input_during_parked_goal(tmp_path, monkeypatch, standby):
    comms, agent, session, _ = await _owner(tmp_path, monkeypatch, standby=standby)
    monkeypatch.setattr(agent, "_schedule_wake", lambda _session: None)
    session_file = tmp_path / "pi-session.jsonl"
    session_file.touch()
    received = tmp_path / "received.jsonl"
    stub = tmp_path / "pi-owner-followup-stub"
    stub.write_text(
        f"#!{sys.executable}\n"
        + f"session_file={str(session_file)!r}\n"
        + f"received={str(received)!r}\n"
        + """
import json, sys
send = lambda event: print(json.dumps(event), flush=True)
count = 0
for line in sys.stdin:
    command = json.loads(line)
    kind = command["type"]
    data = {}
    if kind == "get_state":
        data = {"nativeInputProofCapability":"pi-native-input-v1-live-only",
                "sessionFile":session_file}
    send({"type":"response", "command":kind, "id":command["id"], "success":True, "data":data})
    if kind == "prompt":
        with open(received, "a") as stream:
            stream.write(json.dumps(command) + "\\n")
        send({"type":"message_start", "message":{"role":"user",
              "content":command["message"], "inputId":command["inputId"]}})
        count += 1
        if count == 2:
            send({"type":"message_end", "message":{"role":"assistant", "stopReason":"stop"}})
            send({"type":"agent_settled"})
"""
    )
    stub.chmod(0o755)
    agent._agent_bin = str(stub)
    agent._agent_args = []
    started = asyncio.Event()
    emit = agent._emit_event

    async def capture_started(session_id, event):
        await emit(session_id, event)
        if event.get("type") == "input_started" and event.get("id") is None:
            started.set()

    monkeypatch.setattr(agent, "_emit_event", capture_started)
    goal_before = comms.registry.require(session).goal
    wait_before = comms.goal_wait(session)
    comms.send_message("outsider", session, "Separate DM")
    await agent._drain_owned_inbox(session)
    queued = agent._pending_turns.pop(session)[0]
    turn = asyncio.create_task(
        agent._run_agent_turn(
            session,
            session,
            queued.prompt,
            origins=(queued.origin,),
            direct_interrupt_goal_id=queued.direct_interrupt_goal_id,
            direct_interrupt_goal_revision=queued.direct_interrupt_goal_revision,
            direct_interrupt_wait_id=queued.direct_interrupt_wait_id,
            direct_interrupt_input_key=queued.direct_interrupt_input_key,
            direct_interrupt_ticket=queued.direct_interrupt_ticket,
        )
    )
    try:
        await asyncio.wait_for(started.wait(), 5)
        response = await agent.prompt(
            session, [TextContentBlock(type="text", text="Fresh owner request")]
        )
        public_id = response.field_meta["agentComms"]["inputDisposition"]["inputId"]
        await asyncio.wait_for(turn, 5)
        assert agent._dispositions.status("acp:" + public_id) == "started"
        commands = [json.loads(line) for line in received.read_text().splitlines()]
        assert len(commands) == 2
        assert commands[1]["message"] == "User follow-up:\nFresh owner request"
        assert commands[0]["inputId"] != commands[1]["inputId"]
        assert comms.registry.require(session).goal == goal_before
        assert comms.goal_wait(session) == wait_before
        assert not (comms.root / "goal-private").exists()
    finally:
        turn.cancel()
        await asyncio.gather(turn, return_exceptions=True)
        await agent.shutdown()
