from agent_comms.cli_bridges.codex_protocol import BusInput, ItemsPage
from agent_comms.transcript_events import UserTranscript


def test_items_page_decodes_every_declared_user_input():
    # The JSON-RPC envelope is decoded first; items arrive as decoded JSON.
    page = ItemsPage.model_validate({"data": [{
        "turnId": "t1", "startedAtMs": 1000, "completedAtMs": 2000,
        "item": {"type": "userMessage", "id": "u1", "content": [
            {"type": "text", "text": "look", "text_elements": []},
            {"type": "localImage", "path": "/tmp/shot.png", "detail": None},
            {"type": "mention", "name": "toad", "path": "app://toad"},
        ]},
    }]})
    update = page.data[0].publication()
    assert update.events == (UserTranscript("look\nImage: /tmp/shot.png\n@toad", timestamp=1.0),)


def test_bus_input_encodes_its_text_with_the_codex_tag():
    params = BusInput.for_message("thread", "m1", "hi").model_dump(mode="json")
    assert params["input"] == [{"type": "text", "text": "hi"}]
