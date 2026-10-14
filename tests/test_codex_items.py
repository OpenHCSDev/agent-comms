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


def test_codex_stream_reports_activity_and_finishes_items():
    from agent_comms.activity import ActivityState
    from agent_comms.cli_bridges.codex_protocol import ItemStream, RpcNotification
    from agent_comms.cli_bridges.shared import ActiveItem, FinishedItem, TurnEnded, TurnStarted
    from agent_comms.transcript_events import ToolEndTranscript, ToolStartTranscript

    stream = ItemStream("th")
    command = {"type": "commandExecution", "id": "c1", "command": "ls -la", "status": "inProgress"}

    def receive(method, **params):
        return stream.receive(RpcNotification(method=method, params={"threadId": "th", **params}))

    assert receive("turn/started", turn={"id": "t1"}) == TurnStarted("t1")
    assert receive("item/started", turnId="t1", item=command, startedAtMs=1000) \
        == ActiveItem("t1", "c1", ActivityState.WORKING, "Run ls -la")
    assert receive("item/commandExecution/outputDelta", turnId="t1", itemId="c1",
                   delta="a") is None
    finished = receive("item/completed", turnId="t1", completedAtMs=2000,
                       item=command | {"status": "completed", "exitCode": 0,
                                       "aggregatedOutput": "a\n"})
    assert finished == FinishedItem("t1", "c1", (
        ToolStartTranscript(tool_call_id="c1", tool_name="bash",
                            raw_input={"command": "ls -la"}, timestamp=1.0),
        ToolEndTranscript(tool_call_id="c1", tool_name="bash", text="a\n", ok=True,
                          timestamp=1.0)))
    thinking = {"type": "reasoning", "id": "r1", "summary": []}
    assert receive("item/started", turnId="t1", item=thinking, startedAtMs=3000) \
        == ActiveItem("t1", "r1", ActivityState.THINKING, "")
    assert receive("item/completed", turnId="t1", item=thinking, completedAtMs=4000).events == ()
    assert receive("turn/completed", turn={"id": "t1", "status": "completed"}) == TurnEnded("t1")
