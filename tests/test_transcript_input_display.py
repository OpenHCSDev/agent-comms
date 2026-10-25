"""Saved native input IDs bind transcript display to the owner's submitted intent."""
from agent_comms.repo import Checkout
from agent_comms.controller import SessionUpdateSink

import json
import sqlite3

import pytest

from conftest import transcript_records
from agent_comms.session_file import RegistrySessionLocation
from agent_comms.acp import CommsAgent
from agent_comms.acp_extension import TranscriptSnapshotUpdate, decode_updates
from agent_comms.comms import wire
from conftest import installed_comms
from agent_comms.native_entries import TranscriptProjection
from agent_comms.pi_payloads import PiMessage, UserMessage
from agent_comms.threads import Thread
from agent_comms.transcript_routes import DisplayText, InputDisplay, NoDisplayText, SentInputText
from bus_record_fixture import FIRST_INPUT_ID, SECOND_INPUT_ID, THIRD_INPUT_ID

GOAL_PROMPT = (
    "Persistent goal deadbeef: Read files until stopped.\n"
    "Coordination context: private owner instructions\n\n"
    "Continue working toward the active goal."
)


def saved_row(native_id, text, *, images=False, role="user"):
    content = [{"type": "text", "text": text}]
    if images:
        content.append({"type": "image", "mimeType": "image/png", "data": "secret-image"})
    return transcript_records.message(
        {"role": role, "inputId": str(native_id), "content": content}, id=native_id[:8]
    )


def test_saved_goal_prompt_hidden_but_followup_and_images_survive_reopen(tmp_path):
    comms = installed_comms(tmp_path / "wire")
    comms.registry.declare(Thread('worker', frozenset(), Checkout.from_wire(str(tmp_path))))
    # These annotations are committed before sending, before Pi creates its first file.
    comms.transcripts.routes.record_input_display(FIRST_INPUT_ID, NoDisplayText())
    comms.transcripts.routes.record_input_display(
        SECOND_INPUT_ID, DisplayText("Please inspect this image\nthen continue.")
    )
    # Send now checks the same already-bound ID again; it cannot change its display.
    comms.transcripts.routes.record_input_display(
        SECOND_INPUT_ID, DisplayText("User follow-up: private wrapper"))
    session = tmp_path / "session.jsonl"
    rows = [
        saved_row(FIRST_INPUT_ID, GOAL_PROMPT),
        saved_row(
            SECOND_INPUT_ID, "User follow-up:\nPlease inspect this image\nthen continue.",
            images=True,
        ),
        # A genuine user may quote these exact words; never filter by prefix.
        saved_row(THIRD_INPUT_ID, GOAL_PROMPT),
        saved_row(FIRST_INPUT_ID, "assistant reply", role="assistant"),
    ]
    session.write_text("".join(json.dumps(row) + "\n" for row in rows))
    comms.threads.attach_session((attachment_owner := comms.registry.require("worker")), str(session), process=attachment_owner.process_identity)
    reopened = wire(comms.root)
    events = reopened.transcripts.capture_page_read("worker").read().events
    assert [event.text for event in events if event.declared_name == "context"] == [
        GOAL_PROMPT,
        "User follow-up:\nPlease inspect this image\nthen continue.",
    ]
    assert [
        (event.declared_name, event.text) for event in events if event.declared_name != "context"
    ] == [
        ("user", "Please inspect this image\nthen continue."),
        ("user", "[Image attachment: image/png]"),
        ("user", GOAL_PROMPT),
        ("assistant", "assistant reply"),
    ]


def test_incomplete_durable_annotation_schema_is_not_repaired(tmp_path):
    comms = wire(tmp_path / "wire")
    comms.transcripts.routes.record_input_display(FIRST_INPUT_ID, DisplayText("first input"))
    path = comms.transcripts.routes.path
    with sqlite3.connect(path) as connection:
        connection.execute("DROP TABLE transcript_route")
    before = path.read_bytes()
    reopened = wire(comms.root)
    with pytest.raises(ValueError):
        reopened.transcripts.routes.record_input_display(SECOND_INPUT_ID, NoDisplayText())
    with pytest.raises(ValueError), reopened.transcripts.routes.for_session("session") as routes:
        routes.input_display(FIRST_INPUT_ID)
    assert path.read_bytes() == before


@pytest.mark.asyncio
async def test_acp_saved_transcript_replay_hides_only_owned_internal_input(tmp_path):
    comms = installed_comms(tmp_path / "wire")
    session = tmp_path / "session.jsonl"
    session.write_text(
        "".join(
            json.dumps(row) + "\n"
            for row in [
                saved_row(FIRST_INPUT_ID, GOAL_PROMPT),
                saved_row(SECOND_INPUT_ID, "User follow-up:\ntest2"),
                saved_row(THIRD_INPUT_ID, "Done reading", role="assistant"),
            ]
        )
    )
    comms.registry.declare(Thread('worker', frozenset(), Checkout.from_wire(str(tmp_path)), session_file=RegistrySessionLocation.from_path(str(session), comms.root)))
    comms.transcripts.routes.record_input_display(FIRST_INPUT_ID, NoDisplayText())
    comms.transcripts.routes.record_input_display(SECOND_INPUT_ID, DisplayText("test2"))
    agent = CommsAgent(wire(comms.root))
    updates = []

    class Client(SessionUpdateSink):
        async def publish_update(self, **kwargs):
            updates.append(kwargs["update"])
            return True

    try:
        await agent.sessions.transcript.replay("worker", client=Client())
        facts = decode_updates(updates[0].field_meta)
        snapshot, = (fact for fact in facts if isinstance(fact, TranscriptSnapshotUpdate))
        events = snapshot.page.events
        assert [event.text for event in events if event.declared_name == "context"] == [
            GOAL_PROMPT,
            "User follow-up:\ntest2",
        ]
        assert [
            (event.declared_name, event.text) for event in events if event.declared_name != "context"
        ] == [
            ("user", "test2"),
            ("assistant", "Done reading"),
        ]
    finally:
        await agent.shutdown()


@pytest.mark.parametrize(("content", "expected"), [
    ({}, False), ({"content": None}, False),
    ({"content": ""}, True), ({"content": []}, True),
])
def test_empty_reported_content_is_not_omitted(content, expected):
    message = UserMessage.from_wire({"role": "user", **content})
    display = InputDisplay(
        FIRST_INPUT_ID, NoDisplayText(), sent_text_digest=SentInputText("").digest,
    )
    assert message.matches_display(display.sent_text_digest) is expected
    assert message.to_wire()["content"] == content.get("content")


def test_adjacent_assistant_text_parts_preserve_one_markdown_message(tmp_path):
    events = PiMessage.from_wire(
        {
            "role": "assistant",
            "content": [
                {"type": "text", "text": "## Summary\n\n**Bold"},
                {"type": "text", "text": " text**\n\n- First\n- Second\n"},
                {"type": "thinking", "thinking": "separate reasoning"},
                {"type": "text", "text": "After reasoning."},
            ],
        }
    ).transcript_events(TranscriptProjection(position="assistant"), transcript_records.timestamp.at.timestamp())
    assert [(event.declared_name, event.text) for event in events] == [
        ("assistant", "## Summary\n\n**Bold text**\n\n- First\n- Second\n"),
        ("thinking", "separate reasoning"),
        ("assistant", "After reasoning."),
    ]
