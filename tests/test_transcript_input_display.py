"""Saved native input IDs bind transcript display to the owner's submitted intent."""

import json
import sqlite3

import pytest

from agent_comms import Thread, wire
from agent_comms.acp import CommsAgent

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
    comms.register(Thread("worker", frozenset(), str(tmp_path)))
    # These annotations are committed before sending, before Pi creates its first file.
    comms.record_input_display("a" * 32, None)
    comms.record_input_display("b" * 32, "Please inspect this image\nthen continue.")
    # Send now checks the same already-bound ID again; it cannot change its display.
    comms.record_input_display("b" * 32, "User follow-up: private wrapper")
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
    comms.attach_session("worker", str(session))
    reopened = wire(comms.root)
    events = (
        reopened.thread_transcript_page("worker").events
        if paged
        else reopened.thread_transcript("worker")
    )
    assert [event.text for event in events if event.kind == "context"] == [
        GOAL_PROMPT,
        "User follow-up:\nPlease inspect this image\nthen continue.",
    ]
    assert [(event.kind, event.text) for event in events if event.kind != "context"] == [
        ("user", "Please inspect this image\nthen continue."),
        ("user", "[Image attachment: image/png]"),
        ("user", GOAL_PROMPT),
        ("assistant", "assistant reply"),
    ]


def test_existing_route_database_accepts_new_input_annotations_on_reopen(tmp_path):
    comms = wire(tmp_path / "wire")
    comms.record_input_display("a" * 32, "first input")
    # This is exactly the schema present before input display was introduced.
    with sqlite3.connect(comms.transcript_routes.database_path) as connection:
        connection.execute("DROP TABLE input_display")
    reopened = wire(comms.root)
    reopened.record_input_display("b" * 32, None)
    with reopened.transcript_routes.for_session("new-session.jsonl") as routes:
        assert routes.input_display("a" * 32) is None
        assert routes.input_display("b" * 32).text is None


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
    comms.register(Thread("worker", frozenset(), str(tmp_path), session_file=str(session)))
    comms.record_input_display("a" * 32, None)
    comms.record_input_display("b" * 32, "test2")
    agent = CommsAgent(wire(comms.root))
    updates = []

    class Client:
        transcript_snapshots = True

        async def session_update(self, **kwargs):
            updates.append(kwargs["update"])

    try:
        await agent._replay_transcript("worker", "worker", client=Client())
        events = updates[0].field_meta["agentComms"]["transcript"]
        assert [event["text"] for event in events if event["kind"] == "context"] == [
            GOAL_PROMPT,
            "User follow-up:\ntest2",
        ]
        assert [
            (event["kind"], event["text"]) for event in events if event["kind"] != "context"
        ] == [
            ("user", "test2"),
            ("assistant", "Done reading"),
        ]
    finally:
        await agent.shutdown()


def test_adjacent_assistant_text_parts_preserve_one_markdown_message(tmp_path):
    comms = wire(tmp_path / "wire")
    events = comms._transcript_message_events(
        {
            "role": "assistant",
            "content": [
                {"type": "text", "text": "## Summary\n\n**Bold"},
                {"type": "text", "text": " text**\n\n- First\n- Second\n"},
                {"type": "thinking", "thinking": "separate reasoning"},
                {"type": "text", "text": "After reasoning."},
            ],
        }
    )
    assert [(event.kind, event.text) for event in events] == [
        ("assistant", "## Summary\n\n**Bold text**\n\n- First\n- Second\n"),
        ("thinking", "separate reasoning"),
        ("assistant", "After reasoning."),
    ]
