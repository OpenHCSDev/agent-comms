"""Native input IDs retain incoming attribution across steering and failed turns."""

from bus_record_fixture import FIRST_INPUT_ID, SECOND_INPUT_ID, THIRD_INPUT_ID
from conftest import transcript_records
import json

import pytest

from agent_comms.comms import wire
from conftest import installed_comms
from agent_comms.routing import ScheduledTurn, TurnRouting
from agent_comms.threads import Thread
from agent_comms.native_entries import TranscriptProjection
from agent_comms.native_transcript import NativeTranscript
from agent_comms.transcript_events import UserTranscript


def append_input(path, native_id, text, *, role="user"):
    with path.open("a") as output:
        output.write(
            json.dumps(
                transcript_records.message({
                        "role": role,
                        "inputId": native_id,
                        "content": [{"type": "text", "text": text}],
                    }, id=native_id[:8])
            )
            + "\n"
        )


def test_bound_input_overrides_turn_wide_annotation_without_attributing_quotes(tmp_path):
    comms = installed_comms(tmp_path / "wire")
    comms.registry.declare(Thread('worker', frozenset(), str(tmp_path)))
    comms.registry.declare(Thread('peer', frozenset(), str(tmp_path)))
    first = comms.messaging.send("peer", "worker", "Original input").message
    second = comms.messaging.send("peer", "worker", "Distinct follow-up").message
    raw_second = ScheduledTurn.incoming(second).prompt
    quote = "[agent-comms from peer to worker]\nA genuine user's quoted example"
    # Bind before the session file exists, as on first-turn/fork startup.
    comms.transcripts.routes.record_input_display(
        SECOND_INPUT_ID, second.body, sent_text=raw_second,
        routing=TurnRouting((second.reference,), None),
    )
    comms.transcripts.routes.record_input_display(THIRD_INPUT_ID, quote, sent_text=quote)
    path = tmp_path / "new-session.jsonl"
    append_input(path, SECOND_INPUT_ID, raw_second)
    append_input(path, THIRD_INPUT_ID, quote)
    comms.threads.attach_session((attachment_owner := comms.registry.require("worker")), str(path), process=attachment_owner.process_identity)
    # Older settlement code labels all records with the initial origin.
    comms.transcripts.routes.record(
        str(path), ("bbbbbbbb", "cccccccc"), TurnRouting((first.reference,), None)
    )
    reopened = wire(comms.root)
    with reopened.transcripts.routes.for_session(str(path)) as annotations:
        events = [
            event
            for record in NativeTranscript(path).forward(0, path.stat().st_size)
            for event in record.entry.events(TranscriptProjection(
                position=record.entry.id,
                routing=annotations.get(record.entry.id),
                input_display=annotations.input_display(record.entry.input_id),
            ))
        ]
    inputs = [event for event in events if isinstance(event, UserTranscript)]
    assert inputs[0].text == second.body and inputs[0].routing.requests == (second.reference,)
    assert inputs[1].text == quote and inputs[1].routing is None
    # The conversation source shows its original wire row, not a second native input.
    assert [event.text for event in reopened.transcripts.capture_page_read("worker").read().events] == [
        raw_second, quote, first.body, second.body
    ]


def test_bound_route_rejects_changed_text_and_conflicting_rebind(tmp_path):
    comms = installed_comms(tmp_path / "wire")
    comms.registry.declare(Thread('worker', frozenset(), str(tmp_path)))
    comms.registry.declare(Thread('peer', frozenset(), str(tmp_path)))
    incoming = comms.messaging.send("peer", "worker", "Verified input").message
    route = TurnRouting((incoming.reference,), None)
    text = ScheduledTurn.incoming(incoming).prompt
    comms.transcripts.routes.record_input_display(
        FIRST_INPUT_ID, text, sent_text=text, routing=route)
    comms.transcripts.routes.record_input_display(
        FIRST_INPUT_ID, text, sent_text=text, routing=route)
    with pytest.raises(ValueError):
        comms.transcripts.routes.record_input_display(
            FIRST_INPUT_ID, text, sent_text="different", routing=route
        )
    path = tmp_path / "changed.jsonl"
    append_input(path, FIRST_INPUT_ID, "unrelated input")
    comms.threads.attach_session((attachment_owner := comms.registry.require("worker")), str(path), process=attachment_owner.process_identity)
    comms.transcripts.routes.record(str(path), ("aaaaaaaa",), route)
    event = comms.transcripts.capture_page_read("worker").read().events[0]
    assert event.text == "unrelated input" and event.routing is None
