"""Saved native input IDs bind transcript display to the owner's submitted intent."""

import json
import sqlite3

import pytest

from agent_comms.acp import CommsAgent
from agent_comms.acp_extension import TranscriptSnapshotUpdate, decode_updates
from agent_comms.comms import wire
from agent_comms.native_entries import TranscriptProjection
from agent_comms.pi_payloads import PiMessage
from agent_comms.threads import Thread

GOAL_PROMPT = (
    "Persistent goal deadbeef: Read files until stopped.\n"
    "Coordination context: private owner instructions\n\n"
    "Continue working toward the active goal."
)


def saved_row(native_id, text, *, images=False, role="user"):
    content = [{"type": "text", "text": text}]
    if images:
        content.append({"type": "image", "mimeType": "image/png", "data": "secret-image"})
    return {
        "type": "message",
        "id": native_id[:8],
        "message": {"role": role, "inputId": native_id, "content": content},
    }


@pytest.mark.parametrize("paged", [False, True])
def test_saved_goal_prompt_hidden_but_followup_and_images_survive_reopen(tmp_path, paged):
    comms = wire(tmp_path / "wire")
    comms.threads.register(Thread("worker", frozenset(), str(tmp_path)))
    # These annotations are committed before sending, before Pi creates its first file.
    comms.transcripts.routes.record_input_display("a" * 32, None)
    comms.transcripts.routes.record_input_display(
        "b" * 32, "Please inspect this image\nthen continue."
    )
    # Send now checks the same already-bound ID again; it cannot change its display.
    comms.transcripts.routes.record_input_display("b" * 32, "User follow-up: private wrapper")
    session = tmp_path / "session.jsonl"
    rows = [
        saved_row("a" * 32, GOAL_PROMPT),
        saved_row(
            "b" * 32, "User follow-up:\nPlease inspect this image\nthen continue.", images=True
        ),
        # A genuine user may quote these exact words; never filter by prefix.
        saved_row("c" * 32, GOAL_PROMPT),
        saved_row("a" * 32, "assistant reply", role="assistant"),
    ]
    session.write_text("".join(json.dumps(row) + "\n" for row in rows))
    comms.threads.attach_session("worker", str(session))
    reopened = wire(comms.root)
    events = (
        reopened.transcripts.thread_transcript_page("worker").events
        if paged
        else reopened.transcripts.thread_transcript("worker")
    )
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
    comms.transcripts.routes.record_input_display("a" * 32, "first input")
    path = comms.transcripts.routes.database_path
    with sqlite3.connect(path) as connection:
        connection.execute("DROP TABLE transcript_route")
    before = path.read_bytes()
    reopened = wire(comms.root)
    with pytest.raises(ValueError, match="one-shot durable migration"):
        reopened.transcripts.routes.record_input_display("b" * 32, None)
    with pytest.raises(ValueError, match="one-shot durable migration"):
        reopened.transcripts.routes.input_bindings()
    assert path.read_bytes() == before


@pytest.mark.asyncio
async def test_acp_saved_transcript_replay_hides_only_owned_internal_input(tmp_path):
    comms = wire(tmp_path / "wire")
    session = tmp_path / "session.jsonl"
    session.write_text(
        "".join(
            json.dumps(row) + "\n"
            for row in [
                saved_row("a" * 32, GOAL_PROMPT),
                saved_row("b" * 32, "User follow-up:\ntest2"),
                saved_row("c" * 32, "Done reading", role="assistant"),
            ]
        )
    )
    comms.threads.register(Thread("worker", frozenset(), str(tmp_path), session_file=str(session)))
    comms.transcripts.routes.record_input_display("a" * 32, None)
    comms.transcripts.routes.record_input_display("b" * 32, "test2")
    agent = CommsAgent(wire(comms.root))
    updates = []

    class Client:
        async def session_update(self, **kwargs):
            updates.append(kwargs["update"])

    try:
        await agent.sessions.transcript.replay("worker", "worker", client=Client())
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
    ).transcript_events(TranscriptProjection())
    assert [(event.declared_name, event.text) for event in events] == [
        ("assistant", "## Summary\n\n**Bold text**\n\n- First\n- Second\n"),
        ("thinking", "separate reasoning"),
        ("assistant", "After reasoning."),
    ]
