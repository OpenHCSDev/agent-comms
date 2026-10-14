from agent_comms.cli_bridges.codex_protocol import ItemsPage
from agent_comms.transcript_events import UserTranscript


def test_items_page_decodes_codex_json_arrays():
    # The JSON-RPC envelope is decoded first; items arrive as decoded JSON.
    page = ItemsPage.model_validate({"data": [{
        "turnId": "t1", "startedAtMs": 1000, "completedAtMs": 2000,
        "item": {"type": "userMessage", "id": "u1",
                 "content": [{"type": "text", "text": "hello", "text_elements": []}]},
    }]})
    update = page.data[0].publication()
    assert update.events == (UserTranscript("hello", timestamp=1.0),)
