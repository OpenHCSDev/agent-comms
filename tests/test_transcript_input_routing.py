"""Native input IDs retain incoming attribution across steering and failed turns."""

import json

import pytest

from agent_comms import Thread, TurnRouting, wire
from agent_comms.acp import CommsAgent
from agent_comms.declarations import ScheduledTurn


def append_input(path, native_id, text, *, role="user"):
    with path.open("a") as output:
        output.write(
            json.dumps(
                {
                    "type": "message",
                    "id": native_id[:8],
                    "message": {
                        "role": role,
                        "inputId": native_id,
                        "content": [{"type": "text", "text": text}],
                    },
                }
            )
            + "\n"
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("terminal_ok", [False, True])
@pytest.mark.parametrize("target", ["worker", "#team"])
async def test_original_and_busy_input_keep_distinct_routes(
    tmp_path, monkeypatch, terminal_ok, target
):
    monkeypatch.setenv("AGENT_COMMS_AGENT_MODELS", "test/model")
    comms = wire(tmp_path / "wire")
    agent = CommsAgent(comms, agent_bin="pi", agent_args=["--model", "test/model"])
    monkeypatch.setattr(agent, "_ensure_live_drain", lambda _session: None)

    class Client:
        async def session_update(self, **kwargs):
            pass

    agent.on_connect(Client())
    await agent.new_session(str(tmp_path / "worker"))
    comms.update_tags("worker", add=frozenset({"team"}))
    comms.register(Thread("peer", frozenset({"team"}), str(tmp_path)))
    initial = comms.send_message("peer", "worker", "Initial request")
    await agent._drain_inbox("worker")
    session = tmp_path / "session.jsonl"
    session.touch()
    comms.attach_session("worker", str(session))
    received = [initial]

    async def events(*args, **kwargs):
        with kwargs["send_boundary"](None, "a" * 32, args[2]) as allowed:
            assert allowed
            append_input(session, "a" * 32, args[2])
        assert kwargs["native_start"](None, "a" * 32, args[2])
        yield {"type": "input_started", "id": None}
        incoming = comms.send_message("peer", target, "@worker Follow-up request")
        received.append(incoming)
        assert await agent._drain_inbox("worker") == 1
        command = kwargs["steering_queue"].get_nowait()
        public_id = command["_input_id"] if isinstance(command, dict) else "legacy-steer"
        text = command["message"] if isinstance(command, dict) else command
        with kwargs["send_boundary"](public_id, "b" * 32, text) as allowed:
            assert allowed
            append_input(session, "b" * 32, text)
        assert kwargs["native_start"](public_id, "b" * 32, text)
        yield {"type": "input_started", "id": public_id}
        yield {"type": "settled"}
        yield {"type": "done", "ok": terminal_ok, "text": "done"}

    monkeypatch.setattr("agent_comms.acp.backend.stream_agent_events", events)
    try:
        await agent._run_agent_turn(
            "worker", "worker", ScheduledTurn.incoming(initial).prompt, origins=(initial,)
        )
        reopened = wire(comms.root)
        for events in (
            reopened.thread_transcript("worker"),
            reopened.thread_transcript_page("worker").events,
        ):
            inputs = [event for event in events if event.kind == "user"]
            assert [event.text for event in inputs] == [message.body for message in received]
            assert [
                (
                    event.routing.requests[0].message_id
                    if event.routing and event.routing.requests
                    else None
                )
                for event in inputs
            ] == [message.message_id for message in received]
        assert not agent._pending_turns.get("worker"), "Attributed inputs must not be replayed"
    finally:
        await agent.shutdown()


@pytest.mark.parametrize("paged", [False, True])
def test_bound_input_overrides_turn_wide_annotation_without_attributing_quotes(tmp_path, paged):
    comms = wire(tmp_path / "wire")
    comms.register(Thread("worker", frozenset(), str(tmp_path)))
    comms.register(Thread("peer", frozenset(), str(tmp_path)))
    first = comms.send_message("peer", "worker", "Original input")
    second = comms.send_message("peer", "worker", "Distinct follow-up")
    raw_second = ScheduledTurn.incoming(second).prompt
    quote = "[agent-comms from peer to worker]\nA genuine user's quoted example"
    # Bind before the session file exists, as on first-turn/fork startup.
    comms.record_input_display(
        "b" * 32, raw_second, sent_text=raw_second, routing=TurnRouting((second,), None)
    )
    comms.record_input_display("c" * 32, quote, sent_text=quote)
    path = tmp_path / "new-session.jsonl"
    append_input(path, "b" * 32, raw_second)
    append_input(path, "c" * 32, quote)
    comms.attach_session("worker", str(path))
    # Older settlement code labels all records with the initial origin.
    comms.transcript_routes.record(str(path), ("bbbbbbbb", "cccccccc"), TurnRouting((first,), None))
    reopened = wire(comms.root)
    events = (
        reopened.thread_transcript_page("worker").events
        if paged
        else reopened.thread_transcript("worker")
    )
    inputs = [event for event in events if event.kind == "user"]
    assert inputs[0].text == second.body and inputs[0].routing.requests == (second,)
    assert inputs[1].text == quote and inputs[1].routing is None


def test_bound_route_rejects_changed_text_and_conflicting_rebind(tmp_path):
    comms = wire(tmp_path / "wire")
    comms.register(Thread("worker", frozenset(), str(tmp_path)))
    comms.register(Thread("peer", frozenset(), str(tmp_path)))
    incoming = comms.send_message("peer", "worker", "Verified input")
    route = TurnRouting((incoming,), None)
    text = ScheduledTurn.incoming(incoming).prompt
    comms.record_input_display("a" * 32, text, sent_text=text, routing=route)
    comms.record_input_display("a" * 32, text, sent_text=text, routing=route)
    with pytest.raises(ValueError):
        comms.record_input_display("a" * 32, text, sent_text="different", routing=route)
    path = tmp_path / "changed.jsonl"
    append_input(path, "a" * 32, "unrelated input")
    comms.attach_session("worker", str(path))
    comms.transcript_routes.record(str(path), ("aaaaaaaa",), route)
    event = comms.thread_transcript_page("worker").events[0]
    assert event.text == "unrelated input" and event.routing is None
