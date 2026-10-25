"""A line still being appended is not history yet, so a newer read never stalls on it."""

from agent_comms.repo import Checkout
import json

from agent_comms.session_file import RegistrySessionLocation
from agent_comms.threads import Thread


def line(record):
    return json.dumps(record) + "\n"


def answer(identity, text):
    return {"type": "message", "id": identity, "parentId": None, "timestamp": "2026-10-20T00:00:00.000Z",
            "message": {"role": "assistant", "content": [{"type": "text", "text": text}]}}


def test_newer_read_moves_past_a_line_still_being_appended(tmp_path, private_comms):
    comms = private_comms
    session = tmp_path / "saved.jsonl"
    session.write_text(line({"type": "session", "version": 3, "id": "saved-session",
                             "timestamp": "2026-10-20T00:00:00.000Z"}) + line(answer("first", "First answer")))
    comms.registry.declare(Thread("saved", frozenset(), Checkout.from_wire(str(tmp_path)), session_file=RegistrySessionLocation.from_path(str(session), comms.root)))
    complete = session.stat().st_size
    pending = line(answer("second", "Second answer " + "x" * 200_000))
    with session.open("a") as stream:
        stream.write(pending[: len(pending) // 2])

    newest = comms.transcripts.capture_page_read("saved").read()
    assert newest.after.offset == complete
    through = newest.after
    stalled = comms.transcripts.capture_page_read("saved", after=through, through=through).read()
    assert (stalled.after, stalled.has_newer) == (through, False)

    with session.open("a") as stream:
        stream.write(pending[len(pending) // 2:])
    grown = comms.transcripts.capture_page_read("saved").read().after
    newer = comms.transcripts.capture_page_read("saved", after=through, through=grown).read()
    assert newer.after == grown
    assert [event.text.startswith("Second answer") for event in newer.events] == [True]
