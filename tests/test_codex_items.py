from agent_comms.cli_bridges.codex_protocol import BusInput, ItemsPage
from agent_comms.transcript_events import UserTranscript


def _output(tmp_path):
    from agent_comms.comms import Comms
    from agent_comms.runtime_updates import ThreadOutput
    from agent_comms.thread_execution import ExternalThreadExecution
    from agent_comms.threads import Thread

    comms = Comms(tmp_path)
    comms.registry.declare(comms.registry.new_thread('cli', frozenset(), str(tmp_path), execution=ExternalThreadExecution))
    return ThreadOutput(comms.transcripts.external('cli'), comms.agents)


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
    assert update.events == (UserTranscript("look\nImage: /tmp/shot.png\n@toad", timestamp=1.0, position="u1"),)


def test_bus_input_encodes_its_text_with_the_codex_tag():
    params = BusInput.for_message("thread", "m1", "hi").model_dump(mode="json")
    assert params["input"] == [{"type": "text", "text": "hi"}]


def test_codex_stream_reports_activity_and_finishes_items(tmp_path):
    from agent_comms.turn_phase import ModelWaitPhase, ToolRunningPhase
    from agent_comms.cli_bridges.codex_protocol import ItemStream, RpcNotification
    from agent_comms.runtime_updates import ActiveItem, FinishedItem, TurnEnded, TurnStarted
    from agent_comms.native_tools import BashTool
    from agent_comms.transcript_events import ToolEndTranscript, ToolStartTranscript

    stream = ItemStream("th")
    output = _output(tmp_path)
    command = {"type": "commandExecution", "id": "c1", "command": "ls -la", "status": "inProgress"}

    def receive(method, **params):
        updates = stream.receive(RpcNotification(method=method, params={"threadId": "th", **params}))
        parts = tuple(part for update in updates for part in update.accumulate(output))
        return parts[0] if parts else None

    assert receive("turn/started", turn={"id": "t1"}) == TurnStarted()
    assert receive("item/started", turnId="t1", item=command, startedAtMs=1000) \
        == ActiveItem("t1", "c1", ToolRunningPhase(tools=(("c1", "Run ls -la"),)))
    assert receive("item/commandExecution/outputDelta", turnId="t1", itemId="c1",
                   delta="a") is None
    finished = receive("item/completed", turnId="t1", completedAtMs=2000,
                       item=command | {"status": "completed", "exitCode": 0,
                                       "aggregatedOutput": "a\n"})
    assert finished == FinishedItem("t1", "c1", (
        ToolStartTranscript(position="c1", tool_call_id="c1", tool_name="bash",
                            raw_input={"command": "ls -la"}, tool=BashTool(call_id="c1", command="ls -la"), timestamp=2.0),
        ToolEndTranscript(position="c1", tool_call_id="c1", tool_name="bash", text="a\n", ok=True,
                          timestamp=2.0)))
    thinking = {"type": "reasoning", "id": "r1", "summary": []}
    assert receive("item/started", turnId="t1", item=thinking, startedAtMs=3000) \
        == ActiveItem("t1", "r1", ModelWaitPhase())
    assert receive("item/completed", turnId="t1", item=thinking, completedAtMs=4000).events == ()
    assert receive("turn/completed", turn={"id": "t1", "status": "completed"}) == TurnEnded()


def test_attaching_reports_the_status_codex_gives_its_thread():
    from agent_comms.cli_bridges.codex_protocol import ResumedThread
    from agent_comms.runtime_updates import TurnEnded, TurnStarted

    def resumed(status):
        return ResumedThread.model_validate({"thread": {"id": "th", "status": status}})

    assert resumed({"type": "active", "activeFlags": []}).thread.status.update() == TurnStarted()
    assert resumed({"type": "idle"}).thread.status.update() == TurnEnded()


def test_deltas_require_start_and_finish_retires_the_assembly(tmp_path):
    from agent_comms.cli_bridges.codex_protocol import ItemStream, RpcNotification
    from agent_comms.transcript_events import AssistantTranscript

    stream = ItemStream("th")
    output = _output(tmp_path)

    def receive(method, **params):
        updates = stream.receive(RpcNotification(method=method, params={"threadId": "th", "turnId": "t", **params}))
        parts = tuple(part for update in updates for part in update.accumulate(output))
        return parts[-1] if parts else None

    assert receive("item/agentMessage/delta", itemId="a", delta="suffix") is None
    receive("item/started", item={"type": "agentMessage", "id": "a", "text": ""}, startedAtMs=1000)
    receive("item/agentMessage/delta", itemId="a", delta="Hello")
    assert receive("item/agentMessage/delta", itemId="a", delta=" world").events == (
        AssistantTranscript("Hello world", timestamp=1.0, position="a"),)
    complete = receive("item/completed", item={"type": "agentMessage", "id": "a", "text": "Hello world!"},
                       completedAtMs=2000)
    assert complete.events[0].text == "Hello world!"
    assert receive("item/agentMessage/delta", itemId="a", delta="late") is None
    receive("item/started", item={"type": "reasoning", "id": "r", "summary": []}, startedAtMs=3000)
    receive("item/reasoning/summaryTextDelta", itemId="r", summaryIndex=0, delta="One")
    draft = receive("item/reasoning/summaryTextDelta", itemId="r", summaryIndex=1, delta="Two")
    assert draft.events[0].text == "One\n\nTwo"
    receive("turn/completed", turn={"id": "t"})
    assert receive("item/reasoning/summaryTextDelta", itemId="r", summaryIndex=1, delta="late") is None


def test_coalescing_preserves_completion_and_unrelated_updates():
    from agent_comms.runtime_updates import (
        DraftItem, FinishedItem, RuntimeUpdate, TextDraft, TurnEnded,
    )
    from agent_comms.recorded_time import UnrecordedEventTime
    from agent_comms.transcript_events import AssistantTranscript

    first = DraftItem("t", "a", TextDraft.from_parts(AssistantTranscript, ("H",), UnrecordedEventTime(), "a"))
    newer = DraftItem("t", "a", TextDraft.from_parts(AssistantTranscript, ("Hello",), UnrecordedEventTime(), "a"))
    other = DraftItem("t", "b", TextDraft.from_parts(AssistantTranscript, ("Other",), UnrecordedEventTime(), "b"))
    final = FinishedItem("t", "a", (AssistantTranscript("Hello!", timestamp=1.0, position="a"),))
    end = TurnEnded()
    assert RuntimeUpdate.coalesced([first, other, newer]) == [other, newer]
    assert RuntimeUpdate.coalesced([first, other, newer, final, end]) == [other, final, end]


def test_function_outputs_keep_wire_fields_and_source_clocks_in_page_and_live_decoding(tmp_path):
    from agent_comms.cli_bridges.codex_protocol import (
        FunctionOutputItem, ItemStream, RpcNotification, SendOutputItem,
    )
    from agent_comms.transcript_events import ToolEndTranscript
    from agent_comms.runtime_updates import FinishedItem

    for name, item_type in (("comms_send", SendOutputItem), ("other_tool", FunctionOutputItem)):
        wire = {"type": "functionCallOutput", "id": "out", "name": name, "output": "result"}
        page = ItemsPage.model_validate({"data": [{
            "turnId": "turn", "startedAtMs": 1000, "completedAtMs": 2000, "item": wire,
        }]})
        entry, = page.data
        assert type(entry.item) is item_type
        assert entry.item.model_dump(mode="json") == wire
        expected = () if item_type is SendOutputItem else (
            ToolEndTranscript(position="out", tool_call_id="out", tool_name=name, text="result", timestamp=1.0),)
        assert entry.publication().events == expected

        stream = ItemStream("thread")
        output = _output(tmp_path / name)
        started, = stream.receive(RpcNotification(method="item/started", params={
            "threadId": "thread", "turnId": "turn", "startedAtMs": 1000, "item": wire,
        }))
        started.accumulate(output)
        complete, = stream.receive(RpcNotification(method="item/completed", params={
            "threadId": "thread", "turnId": "turn", "completedAtMs": 2000, "item": wire,
        }))
        finished, = complete.accumulate(output)
        assert finished.events == (() if item_type is SendOutputItem else (
            ToolEndTranscript(position="out", tool_call_id="out", tool_name=name,
                              text="result", timestamp=2.0),))
        assert entry.publication().accumulate(output) == (FinishedItem('turn', 'out', expected),)


def test_function_output_names_remain_required_and_strict():
    import pytest
    from pydantic import ValidationError

    for name_fields in ({}, {"name": None}, {"name": 5}):
        with pytest.raises(ValidationError):
            ItemsPage.model_validate({"data": [{
                "turnId": "turn", "item": {
                    "type": "functionCallOutput", "id": "out", "output": "result", **name_fields,
                },
            }]})
