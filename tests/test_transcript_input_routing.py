"""Native input IDs retain incoming attribution across steering and failed turns."""

import json

import pytest

from agent_comms.comms import wire
from agent_comms.routing import ScheduledTurn, TurnRouting
from agent_comms.threads import Thread


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


@pytest.mark.parametrize("paged", [False, True])
def test_bound_input_overrides_turn_wide_annotation_without_attributing_quotes(tmp_path, paged):
    comms = wire(tmp_path / "wire")
    comms.threads.register(Thread("worker", frozenset(), str(tmp_path)))
    comms.threads.register(Thread("peer", frozenset(), str(tmp_path)))
    first = comms.messaging.send_message("peer", "worker", "Original input")
    second = comms.messaging.send_message("peer", "worker", "Distinct follow-up")
    raw_second = ScheduledTurn.incoming(second).prompt
    quote = "[agent-comms from peer to worker]\nA genuine user's quoted example"
    # Bind before the session file exists, as on first-turn/fork startup.
    comms.transcripts.routes.record_input_display(
        "b" * 32, raw_second, sent_text=raw_second, routing=TurnRouting((second,), None)
    )
    comms.transcripts.routes.record_input_display("c" * 32, quote, sent_text=quote)
    path = tmp_path / "new-session.jsonl"
    append_input(path, "b" * 32, raw_second)
    append_input(path, "c" * 32, quote)
    comms.threads.attach_session("worker", str(path))
    # Older settlement code labels all records with the initial origin.
    comms.transcripts.routes.record(
        str(path), ("bbbbbbbb", "cccccccc"), TurnRouting((first,), None)
    )
    reopened = wire(comms.root)
    events = (
        reopened.transcripts.thread_transcript_page("worker").events
        if paged
        else reopened.transcripts.thread_transcript("worker")
    )
    inputs = [event for event in events if event.declared_name == "user"]
    assert inputs[0].text == second.body and inputs[0].routing.requests == (second,)
    assert inputs[1].text == quote and inputs[1].routing is None


def test_bound_route_rejects_changed_text_and_conflicting_rebind(tmp_path):
    comms = wire(tmp_path / "wire")
    comms.threads.register(Thread("worker", frozenset(), str(tmp_path)))
    comms.threads.register(Thread("peer", frozenset(), str(tmp_path)))
    incoming = comms.messaging.send_message("peer", "worker", "Verified input")
    route = TurnRouting((incoming,), None)
    text = ScheduledTurn.incoming(incoming).prompt
    comms.transcripts.routes.record_input_display("a" * 32, text, sent_text=text, routing=route)
    comms.transcripts.routes.record_input_display("a" * 32, text, sent_text=text, routing=route)
    with pytest.raises(ValueError):
        comms.transcripts.routes.record_input_display(
            "a" * 32, text, sent_text="different", routing=route
        )
    path = tmp_path / "changed.jsonl"
    append_input(path, "a" * 32, "unrelated input")
    comms.threads.attach_session("worker", str(path))
    comms.transcripts.routes.record(str(path), ("aaaaaaaa",), route)
    event = comms.transcripts.thread_transcript_page("worker").events[0]
    assert event.text == "unrelated input" and event.routing is None
