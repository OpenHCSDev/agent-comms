"""Native streaming: external RPC parsing and failure handling."""

from agent_comms.pi_counts import PiCount

import asyncio
import json
import os
import signal
import sys
from contextlib import contextmanager, nullcontext, suppress
from pathlib import Path
from uuid import uuid4

import pytest

import agent_comms.agent_events as ae
import agent_comms.backend as backend
from agent_comms.turn_watchdog import ModelWait
from agent_comms.acp_failure import ProviderRetryResult
from agent_comms.agent_backend import InputContent, InputId, InputRequest, TurnInbox, WhenBusy
from agent_comms.pi_native_backend import PersistentPiSession
from agent_comms.image_inputs import ImageInput
from agent_comms.pi_events import AutoRetryStart
from agent_comms.turn_phase import (
    ModelWaitPhase,
    SettlingStatsPhase,
    ShutdownPhase,
    SummarizationRetryPhase,
    ToolRunningPhase,
)
from agent_comms.native_pi import CAPABILITY
from agent_comms.pi_rpc import PiRpcChannel
from agent_comms.send_outcome import AuthorityChangedSend, GrantedSend, RefusedSend
from agent_comms.pi_vocabulary import ThresholdCompactionReason
from pi_session_turn import one_turn_events


def steer(text: str, input_id: str | None = None) -> InputRequest:
    """A queued steering input as Core hands it to the running turn."""
    return InputRequest(
        input_id=InputId(input_id or f"agent-comms-steer-{uuid4().hex}"),
        content=InputContent(text=text),
        when_busy=WhenBusy.STEER,
    )


pytestmark = [
    pytest.mark.usefixtures("native_rpc_fixture"),
    pytest.mark.skipif(
        sys.platform == "win32", reason="backend tests exec shell-script stubs; POSIX only"
    ),
]


def _stub(tmp_path: Path, body: str, name: str = "pi-stub") -> str:
    stub = tmp_path / name
    stub.write_text(body)
    stub.chmod(0o755)
    return str(stub)


def _replay_stub(tmp_path: Path, lines: str, *, startup: str = "") -> str:
    """Replay recorded RPC lines, answering each ``agent-comms-prompt`` reply with
    the id of the request it names, as Pi does for an owned turn's prompt."""
    return _stub(
        tmp_path,
        f"#!{sys.executable}\n"
        + f"""\
import json, sys
from agent_comms.native_pi import CAPABILITY
from agent_comms.pi_payloads import PiMessage
initial_state = True
prompt_request = None
{startup}for line in {lines.splitlines()!r}:
    record = json.loads(line)
    message = record.get("message", {{}})
    if message.get("role") == "assistant":
        message.setdefault("content", [])
    command = record.get("command")
    correlate = record.get("type") == "response" and (
        record.get("id") == "agent-comms-prompt"
        or (command == "get_state" and initial_state)
        or command == "get_session_stats"
    )
    if correlate:
        for request_line in sys.stdin:
            request = json.loads(request_line)
            if request.get("type") == command:
                record["id"] = request["id"]
                if command == "prompt":
                    prompt_request = request
                break
            print(json.dumps({{
                "id": request.get("id"), "type": "response",
                "command": request.get("type"), "success": True,
                "data": {{"nativeInputProofCapability": CAPABILITY}}
                        if request.get("type") == "get_state" else {{}},
            }}), flush=True)
    if command == "get_state":
        record.setdefault("data", {{}})["nativeInputProofCapability"] = CAPABILITY
        initial_state = False
    if (
        prompt_request is not None
        and record.get("type") == "message_start"
        and message.get("role") == "user"
        and PiMessage.from_wire(message).text == prompt_request.get("message")
    ):
        message.setdefault("inputId", prompt_request.get("inputId"))
    print(json.dumps(record), flush=True)
""",
    )


_NATIVE_PROMPT_START = f"""\
import sys
sys.path.insert(0, {str(Path(__file__).parent)!r})
from pi_stub_protocol import answer_state, answer_stats, next_prompt, read_request
_raw_emit = emit
def emit(value):
    message = value.get("message", {{}})
    if message.get("role") == "assistant":
        message.setdefault("content", [])
    _raw_emit(value)
state_data = {{"nativeInputProofCapability": {json.dumps(CAPABILITY)}}}
answer_state(state_data)
prompt = next_prompt()
emit({{"id": prompt["id"], "type": "response", "command": "prompt", "success": True}})
emit({{"type": "message_start", "message": {{"role": "user",
      "content": prompt["message"], "inputId": prompt["inputId"]}}}})
# A real provider cannot complete in the same write as Pi's input start. Keep
# dynamic watchdog fixtures from collapsing those two protocol phases.
import time as _fixture_time
_fixture_time.sleep(0.01)
"""


async def _rpc_events(
    tmp_path: Path,
    records: list[dict],
    *,
    current_input: bool = True,
) -> list[ae.AgentEvent]:
    # Pi's assistant wire type always carries its content array. Parser cases
    # specify only the fields they vary; the fake child supplies the owned base.
    records = [
        record | {"message": {"content": [], **record["message"]}}
        if record.get("message", {}).get("role") == "assistant"
        else record
        for record in records
    ]
    if current_input:
        # Most parser fixtures model an ordinary accepted prompt. They need a
        # matching user start, not a fabricated assistant final. Tests of the
        # missing/foreign input boundary explicitly opt out.
        records = records.copy()
        prompt_ack = next(
            (
                index
                for index, record in enumerate(records)
                if record.get("type") == "response"
                and record.get("command") == "prompt"
                and record.get("success") is True
            ),
            -1,
        )
        if prompt_ack == -1:
            # The backend sends get_state before prompt. Preserve that order
            # when the fixture includes the state response.
            prompt_ack = (
                1
                if records
                and records[0].get("type") == "response"
                and records[0].get("command") == "get_state"
                else 0
            )
            records.insert(
                prompt_ack,
                {
                    "type": "response",
                    "command": "prompt",
                    "id": "agent-comms-prompt",
                    "success": True,
                },
            )
        records.insert(
            prompt_ack + 1,
            {"type": "message_start", "message": {"role": "user", "content": "t"}},
        )
    lines = "\n".join(json.dumps(record) for record in records)
    stub = _replay_stub(tmp_path, lines)
    return [
        event
        async for event in one_turn_events(
            stub, [], "t", str(tmp_path)
        )
    ]


def _assert_full_rpc_stream(events: list[ae.AgentEvent]) -> None:
    stream_events = [e for e in events if not isinstance(e, ae.NativePhaseChanged)]
    assert [type(e) for e in stream_events] == [
        ae.Chunk,
        ae.Chunk,
        ae.Thinking,
        ae.ToolStart,
        ae.ToolProgress,
        ae.ToolEnd,
        ae.Chunk,
        ae.Done,
    ]
    assert [type(e.phase) for e in events if isinstance(e, ae.NativePhaseChanged)] == [
        ModelWaitPhase,
        ToolRunningPhase,
        ModelWaitPhase,
        SettlingStatsPhase,
    ]
    assert stream_events[2].text == "Checking the workspace"
    tool_start = stream_events[3]
    assert tool_start.name == "bash" and tool_start.id == "t1"
    assert "echo hi" in tool_start.title
    assert stream_events[4].output == "running"
    tool_end = stream_events[5]
    assert tool_end.ok is True and "hi" in tool_end.output
    assert stream_events[-1].text == "hello done" and stream_events[-1].ok is True


class TestRpcParsing:
    def test_default_watchdog_yields_to_pi_provider_idle_authority(self):
        assert ModelWait().budget > backend._PI_0_85_1_PROVIDER_IDLE_TIMEOUT_SECONDS

    @pytest.mark.parametrize(
        ("assistant_stops", "successful"),
        [
            ([], False),
            (["toolUse"], False),
            (["error"], False),
            (["toolUse", "stop"], True),
            (["error", "stop"], True),
            (["stop", "aborted"], False),
            (["stop"], True),
        ],
    )
    async def test_rpc_settled_exit_zero_requires_final_assistant_stop(
        self, tmp_path, assistant_stops, successful
    ):
        assistant_records = []
        for index, stop in enumerate(assistant_stops):
            recovering = stop == "error" and index + 1 < len(assistant_stops)
            if recovering:
                assistant_records.append(
                    {"type": "auto_retry_start", "attempt": 1, "maxAttempts": 2}
                )
            assistant_records.extend((
                {"type": "message_start", "message": {"role": "assistant", "content": []}},
                {"type": "message_end", "message": {
                    "role": "assistant", "content": [], "stopReason": stop,
                }},
            ))
            if recovering:
                assistant_records.append(
                    {"type": "auto_retry_end", "success": True, "attempt": 1}
                )
        events = await _rpc_events(
            tmp_path,
            [
                {
                    "type": "response",
                    "command": "prompt",
                    "id": "agent-comms-prompt",
                    "success": True,
                },
                *assistant_records,
                {"type": "agent_settled"},
                {
                    "type": "response",
                    "command": "get_session_stats",
                    "success": True,
                    "data": {"contextUsage": {}},
                },
            ],
        )
        assert events[-1].ok is successful
        if not successful and assistant_stops[-1:] not in (["error"], ["aborted"]):
            assert events[-1].reason_code == "assistant_final_stop_missing"

    @pytest.mark.parametrize(
        "subsequent_event",
        [
            {"type": "agent_start"},
            {"type": "agent_end", "willRetry": True},
            {"type": "message_start", "message": {"role": "user", "content": "next"}},
            {"type": "message_start", "message": {"role": "assistant"}},
            {"type": "compaction_end", "willRetry": True},
        ],
    )
    async def test_prior_final_cannot_authorize_new_incomplete_run(
        self, tmp_path, subsequent_event
    ):
        events = await _rpc_events(
            tmp_path,
            [
                {"type": "message_end", "message": {"role": "assistant", "stopReason": "stop"}},
                subsequent_event,
                {"type": "agent_settled"},
                {
                    "type": "response",
                    "command": "get_session_stats",
                    "success": True,
                    "data": {"contextUsage": {}},
                },
            ],
        )
        assert events[-1].ok is False
        expected = (
            "unrecognized_followup_input"
            if subsequent_event.get("message", {}).get("role") == "user"
            else "assistant_final_stop_missing"
        )
        assert events[-1].reason_code == expected

    @pytest.mark.parametrize(
        ("user_start", "expected_reason"),
        [
            (None, "current_prompt_input_missing"),
            ({"role": "user", "content": "different task"}, "current_prompt_input_missing"),
            ({"role": "user", "content": [{"type": "text", "text": "t"}]}, None),
        ],
    )
    async def test_prior_assistant_final_never_completes_unstarted_prompt(
        self, tmp_path, user_start, expected_reason
    ):
        records = [
            {"type": "response", "command": "prompt", "id": "agent-comms-prompt", "success": True},
            {"type": "message_end", "message": {"role": "assistant", "stopReason": "stop"}},
        ]
        if user_start is not None:
            records.append({"type": "message_start", "message": user_start})
        records.extend(
            [
                {"type": "agent_settled"},
                {"type": "response", "command": "get_session_stats", "success": True, "data": {}},
            ]
        )
        events = await _rpc_events(tmp_path, records, current_input=False)
        assert events[-1].ok is False
        assert events[-1].reason_code == (expected_reason or "assistant_final_stop_missing")

    @pytest.mark.parametrize(
        ("input_events", "reason_code"),
        [
            (
                [
                    {
                        "type": "response",
                        "command": "prompt",
                        "id": "agent-comms-prompt",
                        "success": True,
                    },
                    {"type": "message_start", "message": {"role": "user", "content": "t"}},
                ],
                None,
            ),
            (
                [
                    {"type": "message_start", "message": {"role": "user", "content": "t"}},
                    {
                        "type": "response",
                        "command": "prompt",
                        "id": "agent-comms-prompt",
                        "success": True,
                    },
                ],
                "current_prompt_input_missing",
            ),
            (
                [
                    {
                        "type": "response",
                        "command": "prompt",
                        "id": "agent-comms-prompt",
                        "success": True,
                    },
                    {"type": "message_start", "message": {"role": "user", "content": "t"}},
                    {"type": "message_start", "message": {"role": "user", "content": "other"}},
                ],
                "unrecognized_followup_input",
            ),
        ],
    )
    async def test_current_prompt_start_must_precede_its_final(
        self, tmp_path, input_events, reason_code
    ):
        events = await _rpc_events(
            tmp_path,
            [
                *input_events,
                {"type": "message_end", "message": {"role": "assistant", "stopReason": "stop"}},
                {"type": "agent_settled"},
                {"type": "response", "command": "get_session_stats", "success": True, "data": {}},
            ],
            current_input=False,
        )
        assert events[-1].ok is (reason_code is None)
        if reason_code is not None:
            assert events[-1].reason_code == reason_code

    @pytest.mark.parametrize("managed_finish", [False, True])
    async def test_large_end_of_turn_record_does_not_fail_completed_reply(
        self, tmp_path, managed_finish
    ):
        stub = _stub(
            tmp_path,
            f"#!{sys.executable}\n"
            + """
import json, sys
from agent_comms.native_pi import CAPABILITY
def emit(value):
    print(json.dumps(value), flush=True)
for line in sys.stdin:
    request = json.loads(line)
    command = request["type"]
    if command == "get_state":
        emit({"type": "response", "command": command, "id": request["id"], "success": True,
              "data": {"nativeInputProofCapability": CAPABILITY}})
    elif command == "prompt":
        emit({"type": "response", "command": "prompt", "id": request["id"], "success": True})
        emit({"type": "message_start", "message": {"role": "user", "content": "task",
                                                  "inputId": request["inputId"]}})
        emit({"type": "message_update", "assistantMessageEvent": {
            "type": "text_delta", "delta": "completed reply"}})
        # Pi includes the turn's messages in one large agent_end record.
        emit({"type": "message_end", "message": {
            "role": "assistant", "content": [], "stopReason": "stop",
        }})
        emit({"type": "agent_end", "messages": [{"role": "assistant", "content": "x" * 1048576}]})
        emit({"type": "agent_settled"})
    elif command == "get_session_stats":
        emit({"type": "response", "command": command, "id": request["id"], "success": True,
            "data": {"contextUsage": {"tokens": 64330, "contextWindow": 1048576}}})
        break
""",
        )
        finish = asyncio.Event() if managed_finish else None
        events = []
        async for event in one_turn_events(
            stub, [], "task", str(tmp_path), finish_event=finish
        ):
            events.append(event)
            if isinstance(event, ae.StreamSettled) and finish is not None:
                finish.set()
        assert [event.text for event in events if isinstance(event, ae.Chunk)] == [
            "completed reply"
        ]
        assert any(
            event.context_used == PiCount.from_wire(64330) for event in events if isinstance(event, ae.ContextUsageObservation)
        )
        assert events[-1] == ae.Done(ok=True, text="completed reply", diagnostic={"exit_code": 0})

    async def test_partial_large_record_survives_cancelled_read(self):
        stream = asyncio.StreamReader(limit=8)
        reader = PiRpcChannel(stream)
        stream.feed_data(b'{"text":"' + b"x" * 100)
        pending = asyncio.create_task(reader.readline())
        while not reader.chunks:
            await asyncio.sleep(0)
        pending.cancel()
        await asyncio.gather(pending, return_exceptions=True)
        stream.feed_data(b'"}\nnext\n')
        stream.feed_eof()
        assert await reader.readline() == b'{"text":"' + b"x" * 100 + b'"}\n'
        assert await reader.readline() == b"next\n"
        assert await reader.readline() == b""

    async def test_stream_failure_reaps_backend_and_steering_task(self, tmp_path):
        pid_file = tmp_path / "child.pid"
        stub = _stub(
            tmp_path,
            f'''#!{sys.executable}
import sys
sys.path.insert(0, {str(Path(__file__).parent)!r})
from pi_stub_protocol import send, answer_state, next_prompt, read_state, request_response, read_request
import json, os, pathlib, sys, time
from agent_comms.native_pi import CAPABILITY
pathlib.Path({str(pid_file)!r}).write_text(str(os.getpid()))

answer_state({{'nativeInputProofCapability': CAPABILITY}})
next_prompt()
print('[]', flush=True)
time.sleep(60)
''',
        )
        owner = asyncio.current_task()
        with pytest.raises(ValueError, match="Pi RPC record has wrong type"):
            async for _ in one_turn_events(
                stub, [], "task", str(tmp_path), steering_queue=TurnInbox()
            ):
                pass
        assert pid_file.exists()
        with pytest.raises(ProcessLookupError):
            os.kill(int(pid_file.read_text()), 0)
        assert owner not in backend.TurnSession.active

    async def test_full_rpc_stream(self, tmp_path):
        """Canned pi rpc stream -> chunk/tool_start/tool_end/done events."""
        rpc_lines = "\n".join(
            [
                '{"type":"response","command":"prompt","id":"agent-comms-prompt","success":true}',
                '{"type":"message_start","message":{"role":"user","content":"task"}}',
                '{"type":"message_update","assistantMessageEvent":{"type":"text_delta","delta":"hel"}}',
                '{"type":"message_update","assistantMessageEvent":{"type":"text_delta","delta":"lo"}}',
                '{"type":"message_update","assistantMessageEvent":'
                '{"type":"thinking_delta","delta":"Checking the workspace"}}',
                '{"type":"tool_execution_start","toolCallId":"t1",'
                '"toolName":"bash","args":{"command":"echo hi"}}',
                '{"type":"tool_execution_update","toolCallId":"t1","toolName":"bash",'
                '"partialResult":{"content":[{"type":"text","text":"running"}]}}',
                '{"type":"tool_execution_end","toolCallId":"t1","toolName":"bash",'
                '"result":{"content":[{"type":"text","text":"hi"}]},"isError":false}',
                '{"type":"message_update",'
                '"assistantMessageEvent":{"type":"text_delta","delta":" done"}}',
                '{"type":"message_end","message":{"role":"assistant","stopReason":"stop"}}',
                '{"type":"agent_settled"}',
            ]
        )
        stub = _replay_stub(tmp_path, rpc_lines)
        events = [e async for e in one_turn_events(stub, [], "task", str(tmp_path))]
        # pi-named stub triggers rpc mode; prompt goes to stdin.
        _assert_full_rpc_stream(events)

    async def test_tool_use_progress_requires_committed_assistant_text(self, tmp_path):
        records = [
            {"type": "response", "command": "prompt", "id": "agent-comms-prompt", "success": True},
            {"type": "message_start", "message": {"role": "user", "content": "task"}},
            {"type": "message_start", "message": {"role": "assistant"}},
            {
                "type": "message_update",
                "assistantMessageEvent": {"type": "text_delta", "delta": "Working"},
            },
            {
                "type": "message_end",
                "message": {
                    "role": "assistant",
                    "stopReason": "toolUse",
                    "content": [{"type": "text", "text": "Working"}],
                },
            },
            {
                "type": "tool_execution_start",
                "toolCallId": "t1",
                "toolName": "bash",
                "args": {"command": "true"},
            },
            {
                "type": "tool_execution_end",
                "toolCallId": "t1",
                "toolName": "bash",
                "result": {"content": []},
                "isError": False,
            },
            {"type": "message_start", "message": {"role": "assistant"}},
            {
                "type": "message_update",
                "assistantMessageEvent": {"type": "text_delta", "delta": "Done"},
            },
            {
                "type": "message_end",
                "message": {
                    "role": "assistant",
                    "stopReason": "stop",
                    "content": [{"type": "text", "text": "Done"}],
                },
            },
            {"type": "agent_settled"},
        ]
        lines = "\n".join(json.dumps(record) for record in records)
        stub = _replay_stub(tmp_path, lines)
        events = [
            event async for event in one_turn_events(stub, [], "task", str(tmp_path))
        ]
        assert [event.text for event in events if isinstance(event, ae.CommittedProgress)] == [
            "Working"
        ]
        assert events[-1].ok is True

    async def test_foreign_tool_use_before_exact_input_start_is_not_progress(self, tmp_path):
        # A typed prompt ACK is not a current-turn start. A previous Pi
        # assistant message must not escape as human-visible progress.
        stub = _stub(
            tmp_path,
            f'''#!/usr/bin/env python3
import sys
sys.path.insert(0, {str(Path(__file__).parent)!r})
from pi_stub_protocol import send, answer_state, next_prompt, read_state, request_response, read_request
import json, sys

answer_state({{'nativeInputProofCapability':'pi-native-input-v1-live-only'}})
prompt=next_prompt()
send({{'type':'response','command':'prompt','id':prompt['id'],'success':True}})
send({{'type':'message_start','message':{{'role':'assistant','content':[]}}}})
send({{'type':'message_update','assistantMessageEvent':{{'type':'text_delta','delta':'foreign earlier turn'}}}})
send({{'type':'message_end','message':{{'role':'assistant','stopReason':'toolUse','content':[{{'type':'text','text':'foreign earlier turn'}}]}}}})
send({{'type':'message_start','message':{{'role':'user','content':'foreign later turn','inputId':'foreign'}}}})
''',
        )
        events = [
            event
            async for event in one_turn_events(
                stub, [], "current prompt", str(tmp_path)
            )
        ]
        assert not [event for event in events if isinstance(event, ae.CommittedProgress)]
        assert any(isinstance(event, ae.Done) and event.ok is False for event in events)

    async def test_rpc_model_and_context_metadata(self, tmp_path):
        rpc_lines = "\n".join(
            [
                '{"type":"response","command":"get_state","success":true,"data":'
                '{"model":{"provider":"openrouter","id":"z-ai/glm","contextWindow":1000},'
                '"sessionName":"work","sessionFile":"/tmp/pi-session.jsonl"}}',
                '{"type":"message_update","usage":{"totalTokens":125},'
                '"assistantMessageEvent":{"type":"text_delta","delta":"ok"}}',
                '{"type":"message_end","message":{"role":"assistant","stopReason":"stop"}}',
                '{"type":"agent_settled"}',
                '{"type":"response","command":"get_session_stats","success":true,"data":'
                '{"contextUsage":{"tokens":200,"contextWindow":1000,"percent":20}}}',
            ]
        )
        stub = _replay_stub(tmp_path, rpc_lines)
        events = [e async for e in one_turn_events(stub, [], "t", str(tmp_path))]
        info = [event for event in events if isinstance(event, ae.AgentInfo)]
        assert info[0].model == "openrouter/z-ai/glm"
        assert info[0].session_file == "/tmp/pi-session.jsonl"
        assert len(info) == 1
        context = [event for event in events if isinstance(event, ae.ContextUsageObservation)]
        assert context[-1].context_used == PiCount.from_wire(200)
        assert context[-1].context_size == PiCount.from_wire(1000)

    @pytest.mark.parametrize("stats", [0, None, 60, 260, "invalid", True, "missing"])
    async def test_final_assistant_usage_survives_unavailable_stats(self, tmp_path, stats):
        records = [
            {
                "type": "response",
                "command": "get_state",
                "success": True,
                "data": {"model": {"contextWindow": 1000}},
            },
            {
                "type": "message_end",
                "message": {
                    "role": "assistant",
                    "stopReason": "toolUse",
                    "usage": {"totalTokens": 150},
                },
            },
            {"type": "message_end", "message": {"role": "assistant", "stopReason": "stop"}},
            {"type": "agent_settled"},
            {
                "type": "response",
                "command": "get_session_stats",
                "success": True,
                "data": {"contextUsage": {} if stats == "missing" else {"tokens": stats}},
            },
        ]
        events = await _rpc_events(tmp_path, records)
        used = [e.context_used for e in events if isinstance(e, ae.ContextUsageObservation)]
        assert used == [150, stats if type(stats) is int and stats > 0 else 150]
        assert isinstance(events[-1], ae.Done) and events[-1].ok is True

    async def test_positive_final_usage_survives_empty_stats_and_final_stop_gate(self, tmp_path):
        events = await _rpc_events(
            tmp_path,
            [
                {
                    "type": "response",
                    "command": "get_state",
                    "success": True,
                    "data": {"model": {"contextWindow": 272000}},
                },
                {
                    "type": "message_end",
                    "message": {
                        "role": "assistant",
                        "stopReason": "stop",
                        "usage": {"totalTokens": 78330},
                    },
                },
                {"type": "agent_settled"},
                {
                    "type": "response",
                    "command": "get_session_stats",
                    "success": True,
                    "data": {"contextUsage": {}},
                },
            ],
        )
        assert [e.context_used for e in events if isinstance(e, ae.ContextUsageObservation)] == [
            PiCount.from_wire(None),
            PiCount.from_wire(78330),
            PiCount.from_wire(78330),
        ]
        assert all(e.context_size == PiCount.from_wire(272000) for e in events if isinstance(e, ae.ContextSizeObservation))
        assert events[-1] == ae.Done(ok=True, text="", diagnostic={"exit_code": 0})

    @pytest.mark.parametrize("nested", [False, True])
    async def test_final_usage_replaces_provisional_even_if_lower(self, tmp_path, nested):
        update = {
            "type": "message_update",
            "message": {"role": "assistant"},
            "assistantMessageEvent": {"type": "text_delta", "delta": "ok"},
        }
        if nested:
            update["message"]["usage"] = {"totalTokens": 210}
        else:
            update["usage"] = {"totalTokens": 210}
        events = await _rpc_events(
            tmp_path,
            [
                update,
                {
                    "type": "message_end",
                    "message": {
                        "role": "assistant",
                        "stopReason": "stop",
                        "usage": {"totalTokens": 150},
                    },
                },
                {"type": "agent_settled"},
                {
                    "type": "response",
                    "command": "get_session_stats",
                    "success": True,
                    "data": {"contextUsage": {"tokens": None}},
                },
            ],
        )
        assert [e.context_used for e in events if isinstance(e, ae.ContextUsageObservation)] == [PiCount.from_wire(210), PiCount.from_wire(150), PiCount.from_wire(150)]

    @pytest.mark.parametrize("prior", [None, 150])
    @pytest.mark.parametrize("invalid", ["missing", 0, -1, True, "200", None, 12.5])
    @pytest.mark.parametrize("stats", [0, None, 55])
    async def test_invalid_successful_final_rolls_back_provisional(
        self, tmp_path, prior, invalid, stats
    ):
        final = {"role": "assistant", "stopReason": "stop"}
        if invalid != "missing":
            final["usage"] = {"totalTokens": invalid}
        records = []
        if prior is not None:
            records.append(
                {
                    "type": "message_end",
                    "message": {
                        "role": "assistant",
                        "stopReason": "toolUse",
                        "usage": {"totalTokens": prior},
                    },
                }
            )
        records.extend(
            [
                {
                    "type": "message_update",
                    "message": {"role": "assistant"},
                    "usage": {"totalTokens": 300},
                    "assistantMessageEvent": {"type": "thinking_delta", "delta": "A"},
                },
                {"type": "message_end", "message": final},
                {"type": "agent_settled"},
                {
                    "type": "response",
                    "command": "get_session_stats",
                    "success": True,
                    "data": {"contextUsage": {"tokens": stats}},
                },
            ]
        )
        events = await _rpc_events(tmp_path, records)
        used = [e.context_used for e in events if isinstance(e, ae.ContextUsageObservation)]
        assert used == (
            ([prior] if prior is not None else []) + [300, prior, 55 if stats == 55 else prior]
        )
        assert events[-1].ok is True

    @pytest.mark.parametrize("failure", ["error", "aborted"])
    async def test_failed_assistant_cannot_promote_its_provisional_usage(self, tmp_path, failure):
        events = await _rpc_events(
            tmp_path,
            [
                {
                    "type": "message_end",
                    "message": {
                        "role": "assistant",
                        "stopReason": "toolUse",
                        "usage": {"totalTokens": 150},
                    },
                },
                {
                    "type": "message_update",
                    "message": {"role": "assistant", "usage": {"totalTokens": 310}},
                    "assistantMessageEvent": {"type": "thinking_delta", "delta": "thinking"},
                },
                {
                    "type": "message_end",
                    "message": {
                        "role": "assistant",
                        "stopReason": failure,
                        "errorMessage": "provider error",
                        "usage": {"totalTokens": 500},
                    },
                },
                {"type": "agent_settled"},
                {
                    "type": "response",
                    "command": "get_session_stats",
                    "success": True,
                    "data": {"contextUsage": {"tokens": 0}},
                },
            ],
        )
        assert [e.context_used for e in events if isinstance(e, ae.ContextUsageObservation)] == [
            PiCount.from_wire(150),
            PiCount.from_wire(310),
            PiCount.from_wire(150),
            PiCount.from_wire(150),
        ]
        assert [e.usage.total_tokens for e in events if isinstance(e, ae.ProviderUsage)] == [
            PiCount.from_wire(150),
            PiCount.from_wire(500),
        ]
        assert events[-1].ok is False

    @pytest.mark.parametrize("tokens", [0, -1, 12.5, True, "120", None])
    async def test_invalid_usage_and_non_assistant_messages_are_not_context(self, tmp_path, tokens):
        events = await _rpc_events(
            tmp_path,
            [
                {
                    "type": "response",
                    "command": "get_state",
                    "success": True,
                    "data": {"model": {"contextWindow": 1000}},
                },
                {
                    "type": "message_update",
                    "message": {"role": "toolResult"},
                    "usage": {"totalTokens": 900},
                    "assistantMessageEvent": {"type": "text_delta", "delta": "ok"},
                },
                {
                    "type": "message_end",
                    "message": {"role": "toolResult", "usage": {"totalTokens": 900}},
                },
                {
                    "type": "message_update",
                    "message": {"role": "assistant", "usage": {"totalTokens": tokens}},
                    "assistantMessageEvent": {"type": "thinking_delta", "delta": "thinking"},
                },
                {
                    "type": "message_end",
                    "message": {
                        "role": "assistant",
                        "stopReason": "stop",
                        "usage": {"totalTokens": tokens},
                    },
                },
                {"type": "agent_settled"},
                {
                    "type": "response",
                    "command": "get_session_stats",
                    "success": True,
                    "data": {"contextUsage": {"tokens": tokens}},
                },
            ],
        )
        assert [e.context_used for e in events if isinstance(e, ae.ContextUsageObservation)] == [PiCount.from_wire(None)]

    @pytest.mark.parametrize(
        "compaction",
        [
            {"result": None, "aborted": False},
            {"result": {"summary": "unused"}, "aborted": True},
        ],
    )
    async def test_failed_or_aborted_compaction_keeps_prior_generation(self, tmp_path, compaction):
        events = await _rpc_events(
            tmp_path,
            [
                {
                    "type": "message_end",
                    "message": {
                        "role": "assistant",
                        "stopReason": "toolUse",
                        "usage": {"totalTokens": 250},
                    },
                },
                {"type": "compaction_start", "reason": "auto"},
                {"type": "compaction_end", **compaction},
                {"type": "agent_settled"},
                {
                    "type": "response",
                    "command": "get_session_stats",
                    "success": True,
                    "data": {"contextUsage": {"tokens": 0}},
                },
            ],
        )
        # An aborted or malformed compaction does not prove the old meter is
        # still current; typed usage stays unknown until fresh stats.
        assert [e.context_used for e in events if isinstance(e, ae.ContextUsageObservation)] == [
            PiCount.from_wire(250),
            PiCount.from_wire(None),
            PiCount.from_wire(None),
            PiCount.from_wire(None),
        ]

    @pytest.mark.parametrize("post_compaction", [False, True])
    async def test_compaction_invalidates_old_usage_and_accepts_new_lower_usage(
        self, tmp_path, post_compaction
    ):
        records = [
            {
                "type": "response",
                "command": "get_state",
                "success": True,
                "data": {"model": {"contextWindow": 1000}},
            },
            {
                "type": "message_end",
                "message": {
                    "role": "assistant",
                    "stopReason": "toolUse",
                    "usage": {"totalTokens": 250},
                },
            },
            {"type": "compaction_start", "reason": "auto"},
            {
                "type": "compaction_end",
                "reason": "auto",
                "result": {},
                "aborted": False,
                "willRetry": False,
            },
        ]
        if post_compaction:
            records += [
                {
                    "type": "message_end",
                    "message": {
                        "role": "assistant",
                        "stopReason": "stop",
                        "usage": {"totalTokens": 40},
                    },
                },
            ]
        records += [
            {"type": "agent_settled"},
            {
                "type": "response",
                "command": "get_session_stats",
                "success": True,
                "data": {"contextUsage": {"tokens": 30 if post_compaction else 0}},
            },
        ]
        events = await _rpc_events(tmp_path, records)
        assert [e.context_used for e in events if isinstance(e, ae.ContextUsageObservation)] == (
            [PiCount.from_wire(None), PiCount.from_wire(250), PiCount.from_wire(None), PiCount.from_wire(None), PiCount.from_wire(40), PiCount.from_wire(30)] if post_compaction else [PiCount.from_wire(None), PiCount.from_wire(250), PiCount.from_wire(None), PiCount.from_wire(None), PiCount.from_wire(None)]
        )

    async def test_positive_pi_stats_after_compaction_need_no_locally_seen_final(self, tmp_path):
        events = await _rpc_events(
            tmp_path,
            [
                {
                    "type": "message_end",
                    "message": {
                        "role": "assistant",
                        "stopReason": "stop",
                        "usage": {"totalTokens": 250},
                    },
                },
                {"type": "compaction_end", "result": {"summary": "kept"}, "aborted": False},
                {"type": "agent_settled"},
                {
                    "type": "response",
                    "command": "get_session_stats",
                    "success": True,
                    "data": {"contextUsage": {"tokens": 35}},
                },
            ],
        )
        assert [e.context_used for e in events if isinstance(e, ae.ContextUsageObservation)] == [PiCount.from_wire(250), PiCount.from_wire(None), PiCount.from_wire(35)]

    @pytest.mark.parametrize("changed_field", ["sessionId", "sessionFile"])
    @pytest.mark.parametrize("final_state_observed", [False, True])
    async def test_stats_identity_change_fails_closed_to_unknown(
        self, tmp_path, changed_field, final_state_observed
    ):
        initial = {
            "sessionId": "first",
            "sessionFile": "/tmp/first.jsonl",
            "model": {"provider": "test", "id": "A", "contextWindow": 1000},
            "nativeInputProofCapability": CAPABILITY,
        }
        final = {
            **initial,
            changed_field: "second",
            "model": {"provider": "test", "id": "B", "contextWindow": 9000},
        }
        records = [
            {"type": "response", "command": "get_state", "success": True, "data": initial},
            {
                "type": "message_end",
                "message": {
                    "role": "assistant",
                    "stopReason": "stop",
                    "usage": {"totalTokens": 250},
                },
            },
            {"type": "compaction_end", "result": {}, "aborted": False},
            {"type": "agent_settled"},
        ]
        if final_state_observed:
            records.append(
                {"type": "response", "command": "get_state", "success": True, "data": final}
            )
        records.append(
            {
                "type": "response",
                "command": "get_session_stats",
                "success": True,
                "data": {**final, "contextUsage": {"tokens": 35, "contextWindow": 8000}},
            }
        )
        owner = asyncio.current_task()
        events = await _rpc_events(tmp_path, records)
        info = [e for e in events if isinstance(e, ae.AgentInfo)]
        context = [e for e in events if isinstance(e, ae.ContextUsageObservation)]
        assert [e.context_used for e in context] == [PiCount.from_wire(250), PiCount.from_wire(None), PiCount.from_wire(None)]
        assert len(info) == 1
        assert all(e.session_file == "/tmp/first.jsonl" for e in info)
        assert all(e.context_size == PiCount.from_wire(1000) for e in context)
        assert all(e.model == "test/A" for e in info)
        assert events[-1].reason_code == "session_identity_uncertain"
        assert events[-1].ok is False
        assert owner not in backend.TurnSession.active

    async def test_in_flight_branch_mutation_evidence_invalidates_usage(self, tmp_path):
        identity = {"sessionId": "first", "sessionFile": "/tmp/first.jsonl"}
        events = await _rpc_events(
            tmp_path,
            [
                {"type": "response", "command": "get_state", "success": True, "data": identity},
                {
                    "type": "message_end",
                    "message": {
                        "role": "assistant",
                        "stopReason": "stop",
                        "usage": {"totalTokens": 250},
                    },
                },
                {
                    "type": "response",
                    "command": "fork",
                    "success": True,
                    "data": {"cancelled": False},
                },
                {"type": "agent_settled"},
                {
                    "type": "response",
                    "command": "get_session_stats",
                    "success": True,
                    "data": {**identity, "contextUsage": {"tokens": 35}},
                },
            ],
        )
        assert [e.context_used for e in events if isinstance(e, ae.ContextUsageObservation)] == [
            PiCount.from_wire(250),
            PiCount.from_wire(None),
        ]
        assert events[-1].reason_code == "session_identity_uncertain"
        assert events[-1].ok is False
        assert not any(isinstance(e, ae.StreamSettled) for e in events)

    @pytest.mark.parametrize(
        "mutation",
        [
            {"type": "response", "command": "fork", "success": False},
            {"type": "response", "command": "fork", "success": True, "data": {"cancelled": True}},
        ],
    )
    async def test_failed_or_cancelled_unsolicited_branch_response_fails_closed(
        self, tmp_path, mutation
    ):
        identity = {"sessionId": "first", "sessionFile": "/tmp/first.jsonl"}
        events = await _rpc_events(
            tmp_path,
            [
                {"type": "response", "command": "get_state", "success": True, "data": identity},
                {
                    "type": "message_end",
                    "message": {
                        "role": "assistant",
                        "stopReason": "stop",
                        "usage": {"totalTokens": 250},
                    },
                },
                mutation,
                {"type": "agent_settled"},
                {
                    "type": "response",
                    "command": "get_session_stats",
                    "success": True,
                    "data": {**identity, "contextUsage": {"tokens": 35}},
                },
            ],
        )
        assert [e.context_used for e in events if isinstance(e, ae.ContextUsageObservation)] == [PiCount.from_wire(250), PiCount.from_wire(None)]
        assert events[-1].reason_code == "session_identity_uncertain"
        assert events[-1].ok is False
        assert not any(isinstance(e, ae.StreamSettled) for e in events)

    @pytest.mark.parametrize(
        "evidence", ["get_state", "get_session_stats", "fork_failed", "clone_cancelled"]
    )
    @pytest.mark.parametrize("abort_pipe_closed", [False, True])
    async def test_unsolicited_rebind_quarantines_subsequent_b_events(
        self, tmp_path, evidence, abort_pipe_closed, monkeypatch
    ):
        stub = _stub(
            tmp_path,
            f'''#!{sys.executable}
import sys
sys.path.insert(0, {str(Path(__file__).parent)!r})
from pi_stub_protocol import send, answer_state, next_prompt, read_state, request_response, read_request
import json, sys

initial = {{"sessionId": "first", "sessionFile": "/tmp/first.jsonl",
           "sessionName": "A", "model": {{"provider": "test", "id": "A", "contextWindow": 1000}},
           "nativeInputProofCapability": "pi-native-input-v1-live-only"}}
changed = {{"sessionId": "second", "sessionFile": "/tmp/second.jsonl",
           "sessionName": "B", "model": {{"provider": "test", "id": "B", "contextWindow": 9000}}}}

answer_state(initial)
prompt = next_prompt()
send({{"id": prompt["id"], "type": "response", "command": "prompt", "success": True}})
send({{"type": "message_start", "message": {{"role": "user",
      "content": prompt["message"], "inputId": prompt["inputId"]}}}})
send({{"type": "message_update", "assistantMessageEvent":
      {{"type": "text_delta", "delta": "A-before"}}}})
send({{"type": "message_end", "message": {{"role": "assistant", "content": [],
      "stopReason": "stop", "usage": {{"totalTokens": 250}}}}}})
pending = next_prompt()
assert pending["message"] == "[peer] pending"
evidence = {repr(evidence)}
if evidence in {{"get_state", "get_session_stats"}}:
    send({{"id": "unsolicited", "type": "response", "command": evidence,
          "success": True, "data": {{**changed, "contextUsage": {{"tokens": 9999}}}}}})
else:
    send({{"id": "unsolicited", "type": "response", "command": evidence.split('_')[0],
          "success": evidence.endswith("cancelled"),
          "data": {{"cancelled": evidence.endswith("cancelled")}}}})
send({{"type": "message_start", "message": {{"role": "user",
      "content": pending["message"], "inputId": pending["inputId"]}}}})
send({{"type": "message_update", "assistantMessageEvent":
      {{"type": "text_delta", "delta": "B-secret"}}}})
send({{"type": "message_update", "assistantMessageEvent":
      {{"type": "thinking_delta", "delta": "B-secret"}}}})
send({{"type": "tool_execution_start", "toolCallId": "B-secret", "toolName": "bash",
      "args": {{"command": "B-secret"}}}})
send({{"type": "tool_execution_update", "toolCallId": "B-secret", "partialResult": "B-secret"}})
send({{"type": "tool_execution_end", "toolCallId": "B-secret", "toolName": "bash",
      "result": {{"content": [{{"type": "text", "text": "B-secret"}}]}}}})
send({{"type": "message_end", "message": {{"role": "assistant", "content": [],
      "stopReason": "error", "errorMessage": "B-secret"}}}})
send({{"type": "auto_retry_start", "attempt": 2, "maxAttempts": 3}})
send({{"type": "agent_settled"}})
for line in sys.stdin:
    if read_request(line)["type"] == "abort":
        send({{"type": "response", "command": "abort", "success": True}})
        break
''',
        )
        abort_write_failed = False
        if abort_pipe_closed:
            original_write = asyncio.StreamWriter.write

            def fail_abort_write(writer, data):
                nonlocal abort_write_failed
                if b'"type": "abort"' in data:
                    abort_write_failed = True
                    raise OSError(9, "Pi closed stdin before abort")
                return original_write(writer, data)

            monkeypatch.setattr(asyncio.StreamWriter, "write", fail_abort_write)
        owner = asyncio.current_task()
        process = None
        events = []
        queue = TurnInbox()
        pending = steer("[peer] pending", "in-1")
        async for event in one_turn_events(
            stub, [], "task", str(tmp_path), steering_queue=queue,
            rpc_abort_grace=0.3,
        ):
            events.append(event)
            if isinstance(event, ae.Chunk):
                process = backend.TurnSession.active[owner].native.proc
                queue.put_nowait(pending)
        assert [e.text for e in events if isinstance(e, ae.Chunk)] == ["A-before"]
        assert [e.context_used for e in events if isinstance(e, ae.ContextUsageObservation)] == [PiCount.from_wire(250), PiCount.from_wire(None)]
        assert all(
            e.session_file != "/tmp/second.jsonl" for e in events if isinstance(e, ae.AgentInfo)
        )
        assert not any(
            type(e)
            in {
                ae.Thinking,
                ae.ToolStart,
                ae.ToolProgress,
                ae.ToolEnd,
                ae.StreamSettled,
                "recovered",
            }
            for e in events
        )
        assert "B-secret" not in repr(events)
        assert [type(e) for e in events].count(ae.Done) == 1
        assert process is not None and process.returncode is not None
        assert events[-1] == ae.Done(
            ok=False,
            text="Pi session identity changed during this turn.",
            reason_code="session_identity_uncertain",
            diagnostic={"exit_code": process.returncode},
        )
        assert owner not in backend.TurnSession.active
        assert not any(isinstance(e, ae.InputStarted) for e in events)
        if abort_pipe_closed:
            assert abort_write_failed
        # A sent input on the old owner cannot be replayed after rebind.
        assert queue.empty()

    async def test_failed_post_compaction_assistant_keeps_context_unknown(self, tmp_path):
        events = await _rpc_events(
            tmp_path,
            [
                {
                    "type": "message_end",
                    "message": {
                        "role": "assistant",
                        "stopReason": "stop",
                        "usage": {"totalTokens": 250},
                    },
                },
                {"type": "compaction_end", "result": {"summary": "kept"}, "aborted": False},
                {
                    "type": "message_update",
                    "message": {"role": "assistant", "usage": {"totalTokens": 70}},
                    "assistantMessageEvent": {"type": "text_delta", "delta": "partial"},
                },
                {
                    "type": "message_end",
                    "message": {
                        "role": "assistant",
                        "stopReason": "error",
                        "usage": {"totalTokens": 400},
                    },
                },
                {"type": "agent_settled"},
                {
                    "type": "response",
                    "command": "get_session_stats",
                    "success": True,
                    "data": {"contextUsage": {"tokens": 0}},
                },
            ],
        )
        assert [e.context_used for e in events if isinstance(e, ae.ContextUsageObservation)] == [
            PiCount.from_wire(250),
            PiCount.from_wire(None),
            PiCount.from_wire(70),
            PiCount.from_wire(None),
            PiCount.from_wire(None),
        ]
        assert events[-1].ok is False

    async def test_rpc_resumes_session_and_forwards_live_prompts(self, tmp_path):
        args_path = tmp_path / "args"
        mode_path = tmp_path / "mode"
        steering_path = tmp_path / "steering"
        session_path = tmp_path / "session.jsonl"
        session_path.write_text(json.dumps({
            "type": "session", "version": 3, "id": "saved",
            "timestamp": "2026-10-14T00:00:00.000Z", "cwd": str(tmp_path),
        }) + "\n")
        stub = _stub(
            tmp_path,
            f"""#!/bin/sh
printf '%s' "$*" > '{args_path}'
IFS= read -r state
IFS= read -r prompt
IFS= read -r mode
printf '%s' "$mode" > '{mode_path}'
IFS= read -r steering
printf '%s' "$steering" > '{steering_path}'
cat <<'EOF'
{{"type":"response","command":"get_state","success":true,"data":{{"sessionFile":"{session_path}"}}}}
{{"type":"agent_settled"}}
{{"type":"response","command":"get_session_stats","success":true,"data":{{"contextUsage":{{}}}}}}
EOF
""",
        )
        queue = TurnInbox()
        queue.put_nowait(steer("[peer] ping"))

        events = [
            event
            async for event in one_turn_events(
                stub,
                [],
                "parent task",
                str(tmp_path),
                session_file=str(session_path),
                steering_queue=queue,
            )
        ]

        assert args_path.exists(), events
        assert f"--session {session_path}" in args_path.read_text()
        assert '"type": "set_steering_mode"' in mode_path.read_text()
        assert "[peer] ping" in steering_path.read_text()
        assert '"streamingBehavior": "steer"' in steering_path.read_text()
        assert any(
            event.session_file == str(session_path)
            for event in events
            if isinstance(event, ae.AgentInfo)
        )

    async def test_rpc_stays_open_for_steered_child_reply(self, tmp_path):
        stub = _stub(
            tmp_path,
            """#!/bin/sh
IFS= read -r state
IFS= read -r prompt
echo '{"type":"agent_settled"}'
IFS= read -r steering
echo '{"type":"message_update","assistantMessageEvent":{"type":"text_delta","delta":"pong"}}'
echo '{"type":"agent_settled"}'
IFS= read -r state_after
IFS= read -r stats
echo '{"type":"response","command":"get_session_stats","success":true,"data":{"contextUsage":{}}}'
""",
        )
        queue = TurnInbox()
        finish = asyncio.Event()
        events = []

        async for event in one_turn_events(
            stub,
            [],
            "coordinate",
            str(tmp_path),
            steering_queue=queue,
            finish_event=finish,
        ):
            events.append(event)
            if isinstance(event, ae.StreamSettled):
                if not any(isinstance(item, ae.Chunk) for item in events):
                    queue.put_nowait(steer("[child] ping"))
                else:
                    finish.set()

        assert [event.text for event in events if isinstance(event, ae.Chunk)] == ["pong"]

    async def test_input_start_deadline_owns_wait_after_prompt_acceptance(self, tmp_path):
        stub = _stub(
            tmp_path,
            f'''#!{sys.executable}
import sys
sys.path.insert(0, {str(Path(__file__).parent)!r})
from pi_stub_protocol import send, answer_state, next_prompt, read_state, request_response, read_request
import json, sys, time

answer_state({{"nativeInputProofCapability": "pi-native-input-v1-live-only"}})
prompt = next_prompt()
send({{"id": prompt["id"], "type": "response", "command": "prompt", "success": True}})
time.sleep(0.2)
send({{"type": "message_start", "message": {{"role": "user", "content": prompt["message"],
      "inputId": prompt["inputId"]}}}})
send({{"type": "message_start", "message": {{"role": "assistant", "content": []}}}})
send({{"type": "message_update", "assistantMessageEvent": {{"type": "text_delta", "delta": "ok"}}}})
send({{"type": "message_end", "message": {{"role": "assistant", "content": [],
      "stopReason": "stop"}}}})
send({{"type": "agent_settled"}})
for line in sys.stdin:
    stats = read_request(line)
    if stats["type"] == "get_session_stats":
        send({{"id": stats["id"], "type": "response", "command": "get_session_stats",
              "success": True, "data": {{"contextUsage": {{}}}}}})
        break
''',
        )
        events = [
            event
            async for event in one_turn_events(
                stub, [], "task", str(tmp_path), model_wait=ModelWait(0.05),
            )
        ]
        assert not [event for event in events if isinstance(event, ae.TurnState)]
        assert events[-1] == ae.Done(ok=True, text="ok", diagnostic={"exit_code": 0})

    async def test_accepted_prompt_stall_is_aborted_without_backend_replay(self, tmp_path):
        abort_log = tmp_path / "abort"
        launch_log = tmp_path / "launches"
        stub = _stub(
            tmp_path,
            f"#!{sys.executable}\n"
            + f"""\
import json, pathlib, sys, time
abort_log = pathlib.Path({str(abort_log)!r})
launch_log = pathlib.Path({str(launch_log)!r})
launch_log.write_text(launch_log.read_text() + "x" if launch_log.exists() else "x")
def emit(value):
    print(json.dumps(value), flush=True)
{_NATIVE_PROMPT_START}
abort = read_request()
abort_log.write_text(abort["type"])
emit({{"id": abort.get("id"), "type": "response", "command": "abort", "success": True}})
time.sleep(60)
""",
        )

        events = [
            event
            async for event in one_turn_events(
                stub,
                [],
                "task",
                str(tmp_path),
                model_wait=ModelWait(0.15),
                rpc_abort_grace=0.1,
            )
        ]

        states = [event.state for event in events if isinstance(event, ae.TurnState)]
        assert states == ["model_stalled", "aborting", "failed"]
        stalled = next(
            event
            for event in events
            if isinstance(event, ae.TurnState) and event.state == "model_stalled"
        )
        assert isinstance(stalled.phase, ModelWaitPhase)
        assert stalled.reason_code == "model_no_progress"
        assert stalled.elapsed_ms >= 20
        # TurnState reports native progress only. Replay authority remains with
        # the original execution's durable ReplayAssessment.
        assert not hasattr(stalled, "replay_safe")
        assert abort_log.read_text() == "abort"
        assert launch_log.read_text() == "x"  # Backend never owns prompt/session replay.
        assert [type(event) for event in events].count(ae.Done) == 1
        assert isinstance(events[-1], ae.Done) and events[-1].ok is False
        assert "no RPC progress" in events[-1].text

    async def test_irrelevant_rpc_traffic_does_not_renew_model_lease(self, tmp_path):
        stub = _stub(
            tmp_path,
            f"#!{sys.executable}\n"
            + """\
import json, sys, time
def emit(value):
    print(json.dumps(value), flush=True)
"""
            + _NATIVE_PROMPT_START
            + """\
for index in range(200):
    emit({"type": "response", "command": "get_state", "success": True, "data": {}})
    emit({"id": f"steering-{index}", "type": "response", "command": "prompt", "success": True})
    time.sleep(0.002)
time.sleep(60)
""",
        )
        started = asyncio.get_running_loop().time()

        events = [
            event
            async for event in one_turn_events(
                stub,
                [],
                "task",
                str(tmp_path),
                model_wait=ModelWait(0.15),
                rpc_abort_grace=0.03,
            )
        ]

        elapsed = asyncio.get_running_loop().time() - started
        states = [event.state for event in events if isinstance(event, ae.TurnState)]
        assert states == ["model_stalled", "aborting", "failed"]
        # Six concurrent subprocess workers can add scheduler latency; the
        # watchdog still has to stop the 60-second child promptly.
        assert elapsed < 1.5

    async def test_retry_acceptance_then_silence_does_not_claim_recovery(self, tmp_path):
        stub = _stub(
            tmp_path,
            f"#!{sys.executable}\n"
            + """\
import json, sys, time
def emit(value):
    print(json.dumps(value), flush=True)
"""
            + _NATIVE_PROMPT_START
            + """\
emit({"type": "auto_retry_start", "attempt": 1, "maxAttempts": 2})
emit({"type": "auto_retry_end", "success": True, "attempt": 1})
abort = read_request()
emit({"id": abort.get("id"), "type": "response", "command": "abort", "success": True})
time.sleep(60)
""",
        )

        events = [
            event
            async for event in one_turn_events(
                stub,
                [],
                "task",
                str(tmp_path),
                model_wait=ModelWait(0.15),
                rpc_abort_grace=0.1,
            )
        ]

        states = [event.state for event in events if isinstance(event, ae.TurnState)]
        assert states == ["retrying", "model_stalled", "aborting", "failed"]
        assert "recovered" not in states
        assert isinstance(events[-1], ae.Done) and events[-1].ok is False

    async def test_retry_recovery_requires_substantive_model_progress(self, tmp_path):
        rpc_lines = "\n".join(
            [
                '{"id":"agent-comms-prompt","type":"response","command":"prompt","success":true}',
                '{"type":"auto_retry_start","attempt":1,"maxAttempts":2}',
                '{"type":"auto_retry_end","success":true,"attempt":1}',
                '{"type":"message_start","message":{"role":"user","content":"task"}}',
                '{"type":"message_update","assistantMessageEvent":'
                '{"type":"text_delta","delta":"recovered reply"}}',
                '{"type":"message_end","message":{"role":"assistant","stopReason":"stop"}}',
                '{"type":"agent_settled"}',
                '{"type":"response","command":"get_session_stats","success":true,"data":'
                '{"contextUsage":{}}}',
            ]
        )
        stub = _replay_stub(tmp_path, rpc_lines)

        events = [e async for e in one_turn_events(stub, [], "task", str(tmp_path))]

        states = [event for event in events if isinstance(event, ae.TurnState)]
        assert [event.state for event in states] == ["retrying", "recovered"]
        assert all(not hasattr(event, "replay_safe") for event in states)
        assert events[-1] == ae.Done(
            ok=True,
            text="recovered reply",
            diagnostic={"exit_code": 0},
            retry_result=ProviderRetryResult(success=True, attempt=1),
        )

    async def test_routine_compaction_emits_no_false_recovery_states(self, tmp_path):
        rpc_lines = "\n".join(
            [
                '{"id":"agent-comms-prompt","type":"response","command":"prompt","success":true}',
                '{"type":"compaction_start","reason":"threshold"}',
                '{"type":"compaction_end","reason":"threshold","result":{"summary":"kept"},'
                '"aborted":false,"willRetry":false}',
                '{"type":"message_start","message":{"role":"user","content":"task"}}',
                '{"type":"message_update","assistantMessageEvent":'
                '{"type":"text_delta","delta":"after compaction"}}',
                '{"type":"message_end","message":{"role":"assistant","stopReason":"stop"}}',
                '{"type":"agent_settled"}',
                '{"type":"response","command":"get_session_stats","success":true,"data":'
                '{"contextUsage":{}}}',
            ]
        )
        stub = _replay_stub(tmp_path, rpc_lines)

        events = [e async for e in one_turn_events(stub, [], "task", str(tmp_path))]

        assert not [event for event in events if isinstance(event, ae.TurnState)]
        assert events[-1] == ae.Done(ok=True, text="after compaction", diagnostic={"exit_code": 0})

    async def test_overflow_compaction_retry_recovers_only_after_model_progress(self, tmp_path):
        rpc_lines = "\n".join(
            [
                '{"id":"agent-comms-prompt","type":"response","command":"prompt","success":true}',
                '{"type":"compaction_start","reason":"overflow"}',
                '{"type":"compaction_end","reason":"overflow","result":{"summary":"kept"},'
                '"aborted":false,"willRetry":true}',
                '{"type":"message_update","assistantMessageEvent":'
                '{"type":"text_delta","delta":"continued"}}',
                '{"type":"agent_settled"}',
                '{"type":"response","command":"get_session_stats","success":true,"data":'
                '{"contextUsage":{}}}',
            ]
        )
        stub = _replay_stub(tmp_path, rpc_lines)

        events = [e async for e in one_turn_events(stub, [], "task", str(tmp_path))]

        states = [event for event in events if isinstance(event, ae.TurnState)]
        assert [event.state for event in states] == ["retrying", "recovered"]
        assert states[0].reason_code == "overflow_compaction_retry"
        assert states[1].reason_code == "overflow_retry_progress"
        assert all(not hasattr(event, "replay_safe") for event in states)

    async def test_pi_bounded_compaction_is_not_a_core_model_stall(self, tmp_path):
        """Pi bounds its own compaction; Core's model-wait backstop does not abort it."""
        stub = _stub(
            tmp_path,
            f'''#!{sys.executable}
import json, sys, time
def emit(value):
    print(json.dumps(value), flush=True)

{_NATIVE_PROMPT_START}
emit({{"type": "compaction_start", "reason": "threshold"}})
time.sleep(0.6)
emit({{"type": "compaction_end", "reason": "threshold", "result": {{"summary": "kept"}},
      "aborted": False, "willRetry": False}})
emit({{"type": "message_update", "assistantMessageEvent": {{"type": "text_delta", "delta": "ok"}}}})
emit({{"type": "message_end", "message": {{"role": "assistant", "stopReason": "stop",
      "content": [{{"type": "text", "text": "ok"}}]}}}})
emit({{"type": "agent_settled"}})
answer_state(state_data)
answer_stats({{"contextUsage": {{}}}})
''',
        )

        events = [
            event
            async for event in one_turn_events(
                stub,
                [],
                "task",
                str(tmp_path),
                model_wait=ModelWait(0.15),
                rpc_abort_grace=0.1,
            )
        ]

        assert not [event for event in events if isinstance(event, ae.TurnState)]
        assert events[-1] == ae.Done(ok=True, text="ok", diagnostic={"exit_code": 0})

    async def test_prestart_compaction_outlives_prompt_start_wait(self, tmp_path, monkeypatch):
        """Pi answers ``prompt`` only after its pre-prompt threshold compaction finishes."""
        monkeypatch.setattr(backend, "PROMPT_START_TIMEOUT_SECONDS", 0.1)
        stub = _stub(
            tmp_path,
            f'''#!{sys.executable}
import sys
sys.path.insert(0, {str(Path(__file__).parent)!r})
from pi_stub_protocol import send, answer_state, next_prompt, read_state, request_response, read_request, answer_stats
import json, sys, time

state_data = {{"nativeInputProofCapability": "pi-native-input-v1-live-only"}}
answer_state(state_data)
prompt = next_prompt()
send({{"type": "compaction_start", "reason": "threshold"}})
time.sleep(0.25)
send({{"type": "compaction_end", "reason": "threshold", "result": {{"summary": "saved"}},
      "aborted": False, "willRetry": False}})
send({{"id": prompt["id"], "type": "response", "command": "prompt", "success": True}})
send({{"type": "message_start", "message": {{"role": "user", "content": prompt["message"],
      "inputId": prompt["inputId"]}}}})
send({{"type": "message_update", "assistantMessageEvent": {{"type": "text_delta", "delta": "ok"}}}})
send({{"type": "message_end", "message": {{"role": "assistant", "stopReason": "stop",
      "content": [{{"type": "text", "text": "ok"}}]}}}})
send({{"type": "agent_settled"}})
answer_state(state_data)
answer_stats({{"contextUsage": {{}}}})
''',
        )
        events = [
            event
            async for event in one_turn_events(
                stub,
                [],
                "task",
                str(tmp_path),
                model_wait=ModelWait(0.5),
            )
        ]
        assert [type(e) for e in events if isinstance(e, ae.CompactionEvent)] == [
            ae.CompactionStart,
            ae.CompactionEnd,
        ]
        assert events[-1] == ae.Done(ok=True, text="ok", diagnostic={"exit_code": 0})

    async def test_summarization_retry_stall_is_bounded_and_non_replayable(self, tmp_path):
        stub = _stub(
            tmp_path,
            f"#!{sys.executable}\n"
            + """\
import json, sys, time
def emit(value):
    print(json.dumps(value), flush=True)
"""
            + _NATIVE_PROMPT_START
            + """\
emit({"type": "summarization_retry_scheduled", "attempt": 1, "maxAttempts": 2})
abort = read_request()
emit({"id": abort.get("id"), "type": "response", "command": "abort", "success": True})
time.sleep(60)
""",
        )

        events = [
            event
            async for event in one_turn_events(
                stub,
                [],
                "task",
                str(tmp_path),
                model_wait=ModelWait(0.15),
                rpc_abort_grace=0.1,
            )
        ]

        states = [event for event in events if isinstance(event, ae.TurnState)]
        assert [event.state for event in states] == [
            "retrying",
            "model_stalled",
            "aborting",
            "failed",
        ]
        stalled = next(
            event
            for event in states
            if isinstance(event, ae.TurnState) and event.state == "model_stalled"
        )
        assert isinstance(stalled.phase, SummarizationRetryPhase)
        assert stalled.reason_code == "summarization_retry_no_progress"
        assert all(not hasattr(event, "replay_safe") for event in states)

    async def test_provider_retry_failure_is_distinct_from_model_silence(self, tmp_path):
        rpc_lines = "\n".join(
            [
                '{"type":"auto_retry_start","attempt":1,"maxAttempts":3,'
                '"errorMessage":"secret raw payload"}',
                '{"type":"auto_retry_end","success":false,"attempt":3,'
                '"finalError":"secret raw payload"}',
                '{"type":"message_end","message":{"role":"assistant","stopReason":"error",'
                '"errorMessage":"provider unavailable"}}',
                '{"type":"agent_settled"}',
            ]
        )
        stub = _stub(
            tmp_path,
            f"#!{sys.executable}\n"
            + "import json, sys\n"
            + "def emit(value):\n    print(json.dumps(value), flush=True)\n"
            + _NATIVE_PROMPT_START
            + f"for line in {rpc_lines!r}.splitlines():\n    emit(json.loads(line))\n"
            + "answer_state(state_data)\nanswer_stats({'contextUsage': {}})\n",
        )
        starts = []

        def native_start(public_id, native_id, text):
            starts.append((public_id, native_id, text))
            return True

        events = [
            event
            async for event in one_turn_events(
                stub,
                [],
                "task",
                str(tmp_path),
                model_wait=ModelWait(0.5),
                native_start=native_start,
            )
        ]

        assert len(starts) == 1 and starts[0][0] is None and starts[0][2] == "task", events
        assert len(starts[0][1]) == 32
        recovery = [event for event in events if isinstance(event, ae.TurnState)]
        assert [event.state for event in recovery] == ["retrying", "failed"], events
        assert isinstance(recovery[0].attempt, AutoRetryStart)
        assert recovery[0].attempt.attempt == 1
        assert recovery[0].attempt.max_attempts == 3
        assert recovery[-1].reason_code == "provider_retry_exhausted"
        assert all("secret" not in str(event) for event in recovery)
        assert not any(event.state == "model_stalled" for event in recovery)
        assert events[-1].ok is False
        assert events[-1].text == "provider unavailable"

    @pytest.mark.parametrize("started", [False, True])
    async def test_watchdog_never_requeues_forwarded_input_after_uncertainty(
        self, tmp_path, started
    ):
        steering_log = tmp_path / "steering"
        stub = _stub(
            tmp_path,
            f"#!{sys.executable}\n"
            + f"""\
import json, pathlib, sys, threading, time
emit_lock = threading.Lock()
def emit(value):
    with emit_lock:
        print(json.dumps(value), flush=True)
{_NATIVE_PROMPT_START}
steering_received = threading.Event()
def keep_model_live_until_steering():
    while not steering_received.wait(0.025):
        emit({{"type": "message_update", "assistantMessageEvent":
              {{"type": "thinking_delta", "delta": "."}}}})
threading.Thread(target=keep_model_live_until_steering, daemon=True).start()
steering = next_prompt()
steering_received.set()
pathlib.Path({str(steering_log)!r}).write_text(json.dumps(steering, sort_keys=True))
emit({{"id": steering["id"], "type": "response", "command": "prompt", "success": True}})
if {started!r}:
    emit({{"type": "message_start", "message": {{"role": "user",
          "content": steering["message"], "inputId": steering["inputId"]}}}})
time.sleep(0.06)
abort = read_request()
emit({{"id": abort.get("id"), "type": "response", "command": "abort", "success": True}})
time.sleep(60)
""",
        )
        original = InputRequest(
            input_id=InputId("queued-1"),
            content=InputContent(text="follow", images=(ImageInput("YWJj", "image/png"),)),
            when_busy=WhenBusy.FOLLOW_UP,
        )
        queue = TurnInbox()
        queue.put_nowait(original)

        events = [
            event
            async for event in one_turn_events(
                stub,
                [],
                "task",
                str(tmp_path),
                steering_queue=queue,
                model_wait=ModelWait(0.15),
                rpc_abort_grace=0.2,
            )
        ]

        sent = steering_log.read_text()
        assert '"_input_id"' not in sent and '"images"' in sent
        started_events = [event for event in events if isinstance(event, ae.InputStarted)]
        assert started_events == ([ae.FollowingInputStarted(id="queued-1")] if started else [])
        assert events[-1].ok is False
        # Start evidence informs disposition, but neither case authorizes an
        # automatic replay of the forwarded provider opportunity.
        assert queue.empty()

    async def test_watchdog_never_times_out_an_active_tool(self, tmp_path):
        stub = _stub(
            tmp_path,
            f"#!{sys.executable}\n"
            + """\
import json, sys, time
def emit(value):
    print(json.dumps(value), flush=True)
"""
            + _NATIVE_PROMPT_START
            + """\
emit({"type": "message_start", "message": {"role": "assistant", "content": []}})
emit({"type": "message_end", "message": {
    "role": "assistant", "content": [], "stopReason": "toolUse",
}})
emit({"type": "tool_execution_start", "toolCallId": "slow", "toolName": "bash", "args": {}})
time.sleep(0.12)
emit({"type": "tool_execution_end", "toolCallId": "slow", "toolName": "bash",
      "result": {"content": [{"type": "text", "text": "finished"}]}, "isError": False})
emit({"type": "message_start", "message": {"role": "assistant", "content": []}})
emit({"type": "message_update", "assistantMessageEvent": {
    "type": "text_delta", "delta": "done after tool"}})
emit({"type": "message_end", "message": {"role": "assistant", "stopReason": "stop"}})
emit({"type": "agent_settled"})
answer_state(state_data)
answer_stats({"contextUsage": {}})
""",
        )

        events = [
            event
            async for event in one_turn_events(
                stub,
                [],
                "task",
                str(tmp_path),
                model_wait=ModelWait(0.15),
            )
        ]

        assert not [event for event in events if isinstance(event, ae.TurnState)]
        assert events[-1] == ae.Done(ok=True, text="done after tool", diagnostic={"exit_code": 0})

    async def test_stall_after_tool_is_failed_without_retry(self, tmp_path):
        launches = tmp_path / "launches"
        stub = _stub(
            tmp_path,
            f"#!{sys.executable}\n"
            + f"""\
import json, pathlib, sys, time
launches = pathlib.Path({str(launches)!r})
launches.write_text(launches.read_text() + "x" if launches.exists() else "x")
def emit(value):
    print(json.dumps(value), flush=True)
{_NATIVE_PROMPT_START}
emit({{"type": "tool_execution_start", "toolCallId": "write", "toolName": "write", "args": {{}}}})
emit({{"type": "tool_execution_end", "toolCallId": "write", "toolName": "write",
      "result": {{"content": []}}, "isError": False}})
for line in sys.stdin:
    if read_request(line)["type"] == "abort":
        emit({{"type": "response", "command": "abort", "success": True}})
        time.sleep(60)
""",
        )

        events = [
            event
            async for event in one_turn_events(
                stub,
                [],
                "task",
                str(tmp_path),
                model_wait=ModelWait(0.15),
                rpc_abort_grace=0.1,
            )
        ]

        states = [event.state for event in events if isinstance(event, ae.TurnState)]
        assert states == ["model_stalled", "aborting", "failed"]
        failed = next(
            event for event in events if isinstance(event, ae.TurnState) and event.state == "failed"
        )
        assert isinstance(failed.phase, ShutdownPhase)
        assert failed.reason_code == "model_no_progress"
        assert not hasattr(failed, "replay_safe")
        assert launches.read_text() == "x"
        assert isinstance(events[-1], ae.Done) and events[-1].ok is False
        assert "no RPC progress" in events[-1].text

    async def test_failed_tool_does_not_fail_recovered_turn(self, tmp_path):
        rpc_lines = "\n".join(
            [
                '{"type":"response","command":"prompt","id":"agent-comms-prompt","success":true}',
                '{"type":"message_start","message":{"role":"user","content":"t"}}',
                '{"type":"tool_execution_start","toolCallId":"t1","toolName":"bash","args":{}}',
                '{"type":"tool_execution_end","toolCallId":"t1","toolName":"bash","result":{"content":[{"type":"text","text":"boom"}]},"isError":true}',
                '{"type":"message_end","message":{"role":"assistant","stopReason":"stop"}}',
                '{"type":"agent_settled"}',
            ]
        )
        stub = _replay_stub(tmp_path, rpc_lines)
        events = [e async for e in one_turn_events(stub, [], "t", str(tmp_path))]
        assert events[-1].ok is True
        tool_ends = [event for event in events if isinstance(event, ae.ToolEnd)]
        assert len(tool_ends) == 1 and tool_ends[0].ok is False


# Native per-input proof tests retained from the PR#1 parent.
class TestNativeInputBinding:
    def test_only_original_response_can_promote_pending_attestation(self):
        from agent_comms.native_attestation import PendingAttestation, AttestationError
        from agent_comms.pi_rpc import PiRpcChannel

        pending = PendingAttestation()
        def response(identity, capability='pi-native-input-v1-live-only'):
            return PiRpcChannel.decode_record((json.dumps({
                'type': 'response', 'command': 'get_state', 'id': identity,
                'success': True, 'data': {'nativeInputProofCapability': capability},
            }) + '\n').encode(), strict=True)
        unrelated = response('another-request')
        assert pending.accept(unrelated) is pending
        assert pending.observe(unrelated.data.require_payload()) is pending
        with pytest.raises(AttestationError):
            pending.accept(response(pending.request.id, 'unsupported'))
        assert pending.accept(response(pending.request.id)).observed

    async def test_preflight_consumes_unsolicited_event_before_correlated_response(self, tmp_path):
        stub = _stub(tmp_path, f'''#!{sys.executable}
import sys
sys.path.insert(0, {str(Path(__file__).parent)!r})
from pi_stub_protocol import send, answer_state, next_prompt, read_state, request_response, read_request

import json, sys

state = read_state()
send({{'type':'extension_ui_request', 'method':'setStatus', 'id':'startup-status',
      'statusKey':'startup', 'statusText':'Extension initialized'}})
send({{'type':'agent_settled'}})
send(request_response(state, {{'nativeInputProofCapability':'pi-native-input-v1-live-only'}}) | {{"id": 'unrelated-query'}})
send(request_response(state, {{'nativeInputProofCapability':'pi-native-input-v1-live-only'}}))
prompt = next_prompt()

send({{'type':'response','command':'prompt','id':prompt['id'],'success':True}})
send({{'type':'message_start','message':{{'role':'user','content':prompt['message'],
                                      'inputId':prompt['inputId']}}}})
send({{'type':'message_end','message':{{'role':'assistant','content':[], 'stopReason':'stop'}}}})
send({{'type':'agent_settled'}})
for line in sys.stdin:
    request = read_request(line)
    if request['type'] == 'get_session_stats':
        send({{'type':'response','command':'get_session_stats','id':request.get('id'),
              'success':True,'data':{{}}}})
        break
''')
        async with asyncio.timeout(4):
            events = [event async for event in one_turn_events(
                stub, [], 'work', str(tmp_path))]
        assert events[-1].ok

    async def test_owner_revoked_after_preflight_never_writes_prompt(self, tmp_path):
        received = tmp_path / "received-prompt"
        stub = _stub(
            tmp_path,
            f'''#!{sys.executable}
import sys
sys.path.insert(0, {str(Path(__file__).parent)!r})
from pi_stub_protocol import send, answer_state, next_prompt, read_state, request_response, read_request

import json, select, sys

answer_state({{
                      "nativeInputProofCapability":"pi-native-input-v1-live-only"
                  }})
if select.select([sys.stdin], [], [], 0.3)[0]:
    line = sys.stdin.readline()
    if line: open({str(received)!r}, "w").write(line)
''',
        )
        checks = []

        @contextmanager
        def send_boundary(public_id, native_id, text):
            checks.append((public_id, native_id, text))
            yield AuthorityChangedSend()

        events = [
            event
            async for event in one_turn_events(
                stub, [], "work", str(tmp_path), send_boundary=send_boundary
            )
        ]
        assert len(checks) == 1 and checks[0][0] is None
        assert len(checks[0][1]) == 32 and checks[0][2] == "work"
        assert not received.exists()
        assert events[-1].ok is False

    async def test_malformed_native_user_content_fails_typed_and_reaps_live_child(self, tmp_path):
        pid_file = tmp_path / "pi.pid"
        stub = _stub(
            tmp_path,
            f'''#!{sys.executable}
import sys
sys.path.insert(0, {str(Path(__file__).parent)!r})
from pi_stub_protocol import send, answer_state, next_prompt, read_state, request_response, read_request

import json, os, signal, sys, time

answer_state({{"nativeInputProofCapability":
                  "pi-native-input-v1-live-only"}})
prompt = next_prompt()
print(json.dumps({{"type":"response","command":"prompt","id":prompt["id"],
                  "success":True}}), flush=True)
print(json.dumps({{"type":"message_start","message":{{"role":"user",
                  "inputId":prompt["inputId"],"content":[{{"type":"text","text":None}}]}}}}),
      flush=True)
open({str(pid_file)!r}, 'w').write(str(os.getpid()))
signal.signal(signal.SIGTERM, lambda *_: None)
while True: time.sleep(0.1)
''',
        )
        observed = []
        with pytest.raises(ValueError):
            async for event in one_turn_events(stub, [], "work", str(tmp_path)):
                observed.append(event)
        assert not [event for event in observed if isinstance(event, ae.Done)]
        assert pid_file.exists()
        with pytest.raises(ProcessLookupError):
            os.kill(int(pid_file.read_text()), 0)
        assert not backend.TurnSession.active

    @pytest.mark.parametrize("exit_mode", ["malformed", "early_close"])
    async def test_steering_forwarder_is_reaped_on_invalid_rpc_or_early_close(
        self, tmp_path, exit_mode
    ):
        pid_file = tmp_path / "pi.pid"
        stub = _stub(
            tmp_path,
            f'''#!{sys.executable}
import sys
sys.path.insert(0, {str(Path(__file__).parent)!r})
from pi_stub_protocol import send, answer_state, next_prompt, read_state, request_response, read_request

import json, os, signal, sys, time

answer_state({{"nativeInputProofCapability":
                  "pi-native-input-v1-live-only"}})
prompt = next_prompt()
print(json.dumps({{"type":"response","command":"prompt","id":prompt["id"],
                  "success":True}}), flush=True)
print(json.dumps({{"type":"message_start","message":{{"role":"user",
                  "inputId":prompt["inputId"],"content":"work"}}}}), flush=True)
open({str(pid_file)!r}, 'w').write(str(os.getpid()))
if {exit_mode!r} == 'malformed':
    print(json.dumps({{"type":"message_start","message":{{"role":"user",
                      "content":[{{"type":"text","text":None}}]}}}}), flush=True)
else:
    print(json.dumps({{"type":"message_update",
                      "assistantMessageEvent":{{"type":"text_delta","delta":"begin"}}}}),
          flush=True)
signal.signal(signal.SIGTERM, lambda *_: None)
while True: time.sleep(0.1)
''',
        )
        queue = TurnInbox()
        stream = one_turn_events(stub, [], "work", str(tmp_path), steering_queue=queue)
        try:
            if exit_mode == "malformed":
                with pytest.raises(ValueError):
                    async with asyncio.timeout(4):
                        async for _ in stream:
                            pass
            else:
                async with asyncio.timeout(4):
                    while not isinstance(await stream.__anext__(), ae.Chunk):
                        pass
                    await stream.aclose()
            assert pid_file.exists()
            with pytest.raises(ProcessLookupError):
                os.kill(int(pid_file.read_text()), 0)
            assert not backend.TurnSession.active
            await asyncio.sleep(0)
            assert not [
                task
                for task in asyncio.all_tasks()
                if task.get_coro().__qualname__.endswith("forward_steering")
            ]
        finally:
            await stream.aclose()
            if pid_file.exists():
                with suppress(ProcessLookupError):
                    os.kill(int(pid_file.read_text()), signal.SIGKILL)

    @pytest.mark.parametrize("preflight", ["eof", "invalid_data", "wrong_id"])
    async def test_inconclusive_capability_preflight_always_returns_typed_failure(
        self, tmp_path, preflight
    ):
        stub = _stub(
            tmp_path,
            f'''#!{sys.executable}
import sys
sys.path.insert(0, {str(Path(__file__).parent)!r})
from pi_stub_protocol import send, answer_state, next_prompt, read_state, request_response, read_request

import json, sys
state = read_state()

case = {preflight!r}
if case != "eof":
    data = "not-a-dict" if case == "invalid_data" else {{
        "nativeInputProofCapability":"pi-native-input-v1-live-only"
    }}
    send(request_response(state, data) | {{"id": state["id"] if case != "wrong_id" else "foreign"}})
''',
        )
        events = [
            event async for event in one_turn_events(stub, [], "work", str(tmp_path))
        ]
        assert [event for event in events if isinstance(event, ae.Done)] == [events[-1]]
        assert events[-1].ok is False
        assert events[-1].reason_code == "pi_input_id_unavailable"
        assert "preflight" in events[-1].text

    @pytest.mark.parametrize(
        ("case", "expected_reason"),
        [
            ("accepted_without_start", "queued_input_start_missing"),
            ("accepted_then_started", None),
            ("rejected_without_start", "queued_input_start_missing"),
            ("unacknowledged", "queued_input_start_missing"),
            ("accepted_then_duplicate", "unrecognized_followup_input"),
            ("trimmed_start", "unrecognized_followup_input"),
        ],
    )
    async def test_queued_steer_needs_its_own_user_start_before_success(
        self, tmp_path, case, expected_reason
    ):
        """An original-only final/stats cannot settle an ACP-acknowledged queued input."""
        stub = _stub(
            tmp_path,
            f'''#!{sys.executable}
import sys
sys.path.insert(0, {str(Path(__file__).parent)!r})
from pi_stub_protocol import send, answer_state, next_prompt, read_state, request_response, read_request, answer_stats

import json, sys

state_data = {{"nativeInputProofCapability":"pi-native-input-v1-live-only"}}
answer_state(state_data)
prompt = next_prompt()
send({{"type":"response", "command":"prompt", "id":prompt["id"], "success":True}})
send({{"type":"message_start", "message":{{"role":"user", "content":prompt["message"],
       "inputId":prompt["inputId"]}}}})

steer = next_prompt()
assert steer["streamingBehavior"] == "steer" and steer["message"] == "inbox update"
case = {case!r}
if case != "unacknowledged":
    send({{"type":"response", "command":"prompt", "id":steer["id"],
           "success":case != "rejected_without_start"}})
if case in ("accepted_then_started", "accepted_then_duplicate", "trimmed_start"):
    event = {{"type":"message_start", "message":{{"role":"user",
             "content":steer["message"] + (" " if case == "trimmed_start" else ""),
             "inputId":steer["inputId"]}}}}
    send(event)
    if case == "accepted_then_duplicate": send(event)
send({{"type":"message_update", "assistantMessageEvent":
       {{"type":"text_delta", "delta":"original only"}}}})
send({{"type":"message_end", "message":{{
    "role":"assistant", "content":[], "stopReason":"stop",
}}}})
send({{"type":"agent_settled"}})
stats_data = {{"contextUsage":{{"tokens":33}}}}
if case == "accepted_then_started":
    answer_state(state_data)
    answer_stats(stats_data)
else:
    # An unstarted input prevents settlement queries. A stray usage reply is
    # still not evidence of its start; close the scripted child afterward.
    send(request_response(steer, stats_data) | {{"command":"get_session_stats"}})
''',
        )
        queue = TurnInbox()
        queue.put_nowait(steer("inbox update"))
        async with asyncio.timeout(4):
            events = [
                event
                async for event in one_turn_events(
                    stub, [], "initial turn", str(tmp_path), steering_queue=queue
                )
            ]
        assert [event for event in events if isinstance(event, ae.Done)] == [events[-1]]
        assert events[-1].ok is (expected_reason is None)
        if expected_reason is None:
            assert events[-1].text == "original only"
        else:
            assert events[-1].reason_code == expected_reason
            assert "original only" not in events[-1].text
        assert not backend.TurnSession.active

    async def test_inbox_queued_after_settled_stays_outside_completed_turn(self, tmp_path):
        stub = _stub(
            tmp_path,
            f'''#!{sys.executable}
import sys
sys.path.insert(0, {str(Path(__file__).parent)!r})
from pi_stub_protocol import send, answer_state, next_prompt, read_state, request_response, read_request, answer_stats

import json, sys

state_data = {{"nativeInputProofCapability":"pi-native-input-v1-live-only"}}
answer_state(state_data)
prompt = next_prompt()
send({{"type":"response","command":"prompt","id":prompt["id"],"success":True}})
send({{"type":"message_start","message":{{"role":"user", "content":prompt["message"],
      "inputId":prompt["inputId"]}}}})
send({{"type":"message_end","message":{{
    "role":"assistant", "content":[], "stopReason":"stop",
}}}})
send({{"type":"agent_settled"}})
answer_state(state_data)
answer_stats({{}})
''',
        )
        queue = TurnInbox()
        finish = asyncio.Event()
        events = []
        async with asyncio.timeout(4):
            async for event in one_turn_events(
                stub, [], "initial turn", str(tmp_path), steering_queue=queue, finish_event=finish
            ):
                events.append(event)
                if isinstance(event, ae.StreamSettled):
                    queue.put_nowait(steer("late inbox update"))
                    finish.set()
        assert events[-1].ok is True
        assert not [event for event in events if isinstance(event, ae.InputStarted)]
        assert [event for event in events if isinstance(event, ae.Done)] == [events[-1]]

    async def test_stock_pi_capability_preflight_sends_no_prompt(self, tmp_path):
        received = tmp_path / "received-prompt"
        stub = _stub(
            tmp_path,
            f'''#!{sys.executable}
import sys
sys.path.insert(0, {str(Path(__file__).parent)!r})
from pi_stub_protocol import send, answer_state, next_prompt, read_state, request_response, read_request

import json, select, sys

answer_state({{}})
if select.select([sys.stdin], [], [], 0.2)[0]:
    line = sys.stdin.readline()
    if line: open({str(received)!r}, "w").write(line)
''',
        )
        events = [
            event async for event in one_turn_events(stub, [], "work", str(tmp_path))
        ]
        assert events[-1].ok is False
        assert "native input-ID capability" in events[-1].text
        assert events[-1].reason_code == "pi_input_id_unavailable"
        assert not received.exists()

    async def test_preflight_exit_reports_startup_cause_without_sending_prompt(self, tmp_path):
        received = tmp_path / "received-prompt"
        cause = (
            "Failed to load extension: Native import boundary refused: "
            "outside committed deployment root"
        )
        stub = _stub(
            tmp_path,
            f'''#!{sys.executable}
import sys
sys.path.insert(0, {str(Path(__file__).parent)!r})
from pi_stub_protocol import send, answer_state, next_prompt, read_state, request_response, read_request

import json, select, sys
state = read_state()

print({cause!r}, file=sys.stderr, flush=True)
if select.select([sys.stdin], [], [], 0.1)[0]:
    line = sys.stdin.readline()
    if line: open({str(received)!r}, "w").write(line)
sys.exit(1)
''',
        )
        events = [
            event
            async for event in one_turn_events(
                stub, [], "private unsent prompt", str(tmp_path)
            )
        ]
        done = events[-1]
        assert not done.ok
        assert done.reason_code == "pi_input_id_unavailable"
        assert done.diagnostic["reason"] == "native_preflight_exit"
        assert cause in done.text
        assert "ended before attestation" in done.text
        assert "private unsent prompt" not in done.text
        assert cause not in str(done.diagnostic)
        assert not received.exists()

    async def test_preflight_timeout_reports_phase_duration_and_session_size(
        self, tmp_path, monkeypatch
    ):
        session_file = tmp_path / "session.jsonl"
        session_file.write_text(json.dumps({
            "type": "session", "version": 3, "id": "saved",
            "timestamp": "2026-10-14T00:00:00.000Z", "cwd": str(tmp_path),
        }) + "\n")
        session_size = session_file.stat().st_size
        received = tmp_path / "received-prompt"
        stub = _stub(
            tmp_path,
            f'''#!{sys.executable}
import sys
sys.path.insert(0, {str(Path(__file__).parent)!r})
from pi_stub_protocol import send, answer_state, next_prompt, read_state, request_response, read_request

import json, select, sys, time
state = read_state()

print("Native startup timeout witness", file=sys.stderr, flush=True)
time.sleep(0.3)
if select.select([sys.stdin], [], [], 0)[0]:
    line = sys.stdin.readline()
    if line: open({str(received)!r}, "w").write(line)
''',
        )
        monkeypatch.setattr(backend, "CAPABILITY_PREFLIGHT_TIMEOUT_SECONDS", 0.05)
        events = [
            event
            async for event in one_turn_events(
                stub, [], "secret prompt", str(tmp_path), session_file=str(session_file)
            )
        ]
        done = events[-1]
        assert done.ok is False
        assert done.reason_code == "pi_input_id_unavailable"
        assert "phase=await_get_state" in done.text
        assert f"session_bytes={session_size}" in done.text
        assert done.diagnostic["reason"] == "native_preflight_timeout"
        assert done.diagnostic["session_bytes"] == session_size
        assert done.diagnostic["wait_ms"] >= 0
        assert "elapsed_ms=" in done.text
        assert "wait_ms=" in done.text
        assert "spawn_ms=" in done.text
        assert "Native startup timeout witness" in done.text
        assert "secret prompt" not in done.text
        assert not received.exists()

    async def test_large_saved_session_gets_only_bounded_preflight_budget(
        self, tmp_path, monkeypatch
    ):
        from agent_comms.native_startup import NativeStartup, NativeStartupPolicy

        session = tmp_path / "session.jsonl"
        session.write_text(json.dumps({
            "type": "session", "version": 3, "id": "saved",
            "timestamp": "2026-10-14T00:00:00.000Z", "cwd": str(tmp_path),
        }) + "\n")
        initial_size = session.stat().st_size
        policy = NativeStartupPolicy(
            readiness_seconds=0.05,
            readiness_step_bytes=initial_size,
            readiness_step_seconds=0.2,
            readiness_max_seconds=0.4,
        )
        monkeypatch.setattr(backend, "NATIVE_STARTUP_POLICY", policy)
        monkeypatch.setattr(backend, "CAPABILITY_PREFLIGHT_TIMEOUT_SECONDS", 0.05)
        received = tmp_path / "received-prompt"
        stub = _stub(
            tmp_path,
            f'''#!{sys.executable}
import sys
sys.path.insert(0, {str(Path(__file__).parent)!r})
from pi_stub_protocol import send, answer_state, next_prompt, read_state, request_response, read_request

import json, select, sys, time
state = read_state()

time.sleep(0.12)
send(request_response(state, {{"nativeInputProofCapability":
                  {CAPABILITY!r}, "sessionId":"saved",
                  "sessionFile":{str(session)!r}}}))
if select.select([sys.stdin], [], [], 0.15)[0]:
    line = sys.stdin.readline()
    if line: open({str(received)!r}, "w").write(line)
''',
        )
        small = [
            event
            async for event in one_turn_events(
                stub,
                [],
                "secret prompt",
                str(tmp_path),
                session_file=str(session),
                env_extra={"AGENT_COMMS_ROOT": str(tmp_path)},
            )
        ]
        assert small[-1].diagnostic["budget_ms"] == 50
        assert small[-1].diagnostic["reason"] == "native_preflight_timeout"
        session.write_text(session.read_text() + json.dumps({
            "type": "message", "id": "new", "parentId": None,
            "timestamp": "2026-10-14T00:00:01.000Z",
            "message": {"role": "user", "content": "changed"},
        }) + "\n")
        # The send-boundary probe must not inherit the deliberate timeout delay.
        fast_stub = _stub(
            tmp_path,
            f'''#!{sys.executable}
import sys
sys.path.insert(0, {str(Path(__file__).parent)!r})
from pi_stub_protocol import send, answer_state, next_prompt, read_state, request_response, read_request

import json, select, sys

answer_state({{"nativeInputProofCapability":
                  {CAPABILITY!r}, "sessionId":"saved",
                  "sessionFile":{str(session)!r}}})
if select.select([sys.stdin], [], [], 0.15)[0]:
    line = sys.stdin.readline()
    if line: open({str(received)!r}, "w").write(line)
''',
            name="pi-fast-stub",
        )
        large = [
            event
            async for event in one_turn_events(
                fast_stub,
                [],
                "secret prompt",
                str(tmp_path),
                session_file=str(session),
                env_extra={"AGENT_COMMS_ROOT": str(tmp_path)},
                send_boundary=lambda *_: nullcontext(AuthorityChangedSend()),
            )
        ]
        assert large[-1].ok is False
        assert "Input authority changed before native send." in large[-1].text
        assert large[-1].reason_code == "input_authority_changed"
        assert not received.exists()
        # Startup is independently owned per native child; no global slot remains.
        assert NativeStartup(tmp_path).root == tmp_path

    async def test_persistent_pi_reuses_one_child_with_fresh_prompt_receipts(
        self, tmp_path, monkeypatch
    ):
        session_file = tmp_path / "session.jsonl"
        session_file.write_text(json.dumps({
            "type": "session", "version": 3, "id": "fixed-session",
            "timestamp": "2026-10-14T00:00:00.000Z", "cwd": str(tmp_path),
        }) + "\n")
        proof_file = tmp_path / "session.jsonl.input-proof"
        proof_file.write_text("proof\n")
        ids_file = tmp_path / "prompt-ids"
        stats_marker = tmp_path / "stats-delayed"
        stub = _stub(
            tmp_path,
            f"#!{sys.executable}\n"
            + f"""
import json, os, sys, time
send = lambda event: print(json.dumps(event), flush=True)
delayed_stats = False
for line in sys.stdin:
    command = json.loads(line)
    kind = command["type"]
    if kind == "get_state":
        session_id = json.loads(open({str(session_file)!r}).readline())["id"]
        send({{"type":"response","command":kind,"id":command.get("id"),
               "success":True,"data":{{"nativeInputProofCapability":
               "pi-native-input-v1-live-only","sessionId":session_id,
               "sessionFile":{str(session_file)!r}}}}})
    elif kind == "prompt":
        with open({str(ids_file)!r}, "a") as output:
            output.write(command["id"] + " " + command["inputId"] + "\\n")
        if command["message"] == "cancel":
            time.sleep(5)
            continue
        if command.get("images"):
            sys.stderr.write(command["images"][0]["data"] + "\\n")
            sys.stderr.flush()
        if command["message"] == "fail after image":
            sys.stderr.write("nonimage failure\\n")
            sys.stderr.flush()
            sys.exit(1)
        delayed_stats = command["message"] == "delayed stats"
        send({{"type":"response","command":kind,"id":command["id"],"success":True}})
        send({{"type":"message_start","message":{{"role":"user",
               "content":command["message"],"inputId":command["inputId"]}}}})
        send({{"type":"message_update","assistantMessageEvent":{{
               "type":"text_delta","delta":"ok"}}}})
        send({{"type":"message_end","message":{{"role":"assistant", "content":[],
               "stopReason":"stop"}}}})
        send({{"type":"agent_settled"}})
    elif kind == "get_session_stats":
        if delayed_stats:
            open({str(stats_marker)!r}, "w").write("waiting")
            time.sleep(0.3)
        send({{"type":"response","command":kind,"id":command.get("id"),
               "success":True,"data":{{"contextUsage":{{"tokens":10}}}}}})
""",
        )
        persistent = PersistentPiSession()
        try:
            first = [
                event
                async for event in backend.stream_agent_events(
                    stub,
                    [],
                    "first",
                    str(tmp_path),
                    root=Path(str(tmp_path)), session_file=str(session_file),
                    persistent_session=persistent,
                )
            ]
            first_proc = persistent.child_state.child.proc
            second = [
                event
                async for event in backend.stream_agent_events(
                    stub,
                    [],
                    "second",
                    str(tmp_path),
                    root=Path(str(tmp_path)), session_file=str(session_file),
                    persistent_session=persistent,
                )
            ]
            assert first[-1].ok is True and second[-1].ok is True, second
            assert persistent.child_state.child.proc is first_proc and first_proc is not None
            assert first_proc.returncode is None
            ids = [line.split() for line in ids_file.read_text().splitlines()]
            assert len(ids) == 2
            assert ids[0][0] != ids[1][0] and ids[0][1] != ids[1][1]
            session_file.write_text(json.dumps({
                "type": "session", "version": 3, "id": "changed-session",
                "timestamp": "2026-10-14T00:00:01.000Z", "cwd": str(tmp_path),
            }) + "\n")
            third = [
                event
                async for event in backend.stream_agent_events(
                    stub,
                    [],
                    "third",
                    str(tmp_path),
                    root=Path(str(tmp_path)), session_file=str(session_file),
                    persistent_session=persistent,
                )
            ]
            assert third[-1].ok is True
            assert persistent.child_state.child.proc is not first_proc
            assert first_proc.returncode is not None
            third_proc = persistent.child_state.child.proc
            proof_file.write_text("outside writer changed the proof\n")
            proof_changed = [
                event
                async for event in backend.stream_agent_events(
                    stub,
                    [],
                    "proof changed",
                    str(tmp_path),
                    root=Path(str(tmp_path)), session_file=str(session_file),
                    persistent_session=persistent,
                )
            ]
            assert proof_changed[-1].ok is True
            assert persistent.child_state.child.proc is not third_proc
            assert third_proc is not None and third_proc.returncode is not None
            fourth_proc = persistent.child_state.child.proc

            async def cancelled_turn():
                return [
                    event
                    async for event in backend.stream_agent_events(
                        stub,
                        [],
                        "cancel",
                        str(tmp_path),
                        root=Path(str(tmp_path)), session_file=str(session_file),
                        persistent_session=persistent,
                    )
                ]

            cancelled_task = asyncio.create_task(cancelled_turn())
            async with asyncio.timeout(2):
                while len(ids_file.read_text().splitlines()) < 5:
                    await asyncio.sleep(0.01)
            cancelled_task.cancel()
            with suppress(asyncio.CancelledError):
                await cancelled_task
            assert fourth_proc is not None and fourth_proc.returncode is not None
            after = [
                event
                async for event in backend.stream_agent_events(
                    stub,
                    [],
                    "after",
                    str(tmp_path),
                    root=Path(str(tmp_path)), session_file=str(session_file),
                    persistent_session=persistent,
                )
            ]
            assert after[-1].ok is True
            assert len(ids_file.read_text().splitlines()) == 6

            @contextmanager
            def revoked_goal(public_id, native_id, text):
                assert public_id is None and len(native_id) == 32 and text == "denied"
                yield AuthorityChangedSend()

            denied = [
                event
                async for event in backend.stream_agent_events(
                    stub,
                    [],
                    "denied",
                    str(tmp_path),
                    root=Path(str(tmp_path)), session_file=str(session_file),
                    persistent_session=persistent,
                    send_boundary=revoked_goal,
                )
            ]
            assert denied[-1].ok is False
            assert len(ids_file.read_text().splitlines()) == 6
            revived = [
                event
                async for event in backend.stream_agent_events(
                    stub,
                    [],
                    "revive",
                    str(tmp_path),
                    root=Path(str(tmp_path)), session_file=str(session_file),
                    persistent_session=persistent,
                )
            ]
            assert revived[-1].ok is True
            before_image_proc = persistent.child_state.child.proc
            image = ImageInput("U0VDUkVUX0lNQUdFX0JZVEVT", "image/png")
            image_turn = [
                event
                async for event in backend.stream_agent_events(
                    stub,
                    [],
                    "image turn",
                    str(tmp_path),
                    root=Path(str(tmp_path)), session_file=str(session_file),
                    persistent_session=persistent,
                    images=(image,),
                )
            ]
            assert image_turn[-1].ok is True
            assert persistent.child_state.child.proc is before_image_proc and before_image_proc is not None
            failed = [
                event
                async for event in backend.stream_agent_events(
                    stub,
                    [],
                    "fail after image",
                    str(tmp_path),
                    root=Path(str(tmp_path)), session_file=str(session_file),
                    persistent_session=persistent,
                )
            ]
            assert failed[-1].ok is False
            assert "diagnostics withheld" in failed[-1].text
            assert image.data not in failed[-1].text
            revived = [
                event
                async for event in backend.stream_agent_events(
                    stub,
                    [],
                    "revive again",
                    str(tmp_path),
                    root=Path(str(tmp_path)), session_file=str(session_file),
                    persistent_session=persistent,
                )
            ]
            assert revived[-1].ok is True
            borrowed_proc = persistent.child_state.child.proc

            async def delayed_turn():
                return [
                    event
                    async for event in backend.stream_agent_events(
                        stub,
                        [],
                        "delayed stats",
                        str(tmp_path),
                        root=Path(str(tmp_path)), session_file=str(session_file),
                        persistent_session=persistent,
                    )
                ]

            active = asyncio.create_task(delayed_turn())
            async with asyncio.timeout(2):
                while not stats_marker.exists():
                    await asyncio.sleep(0.01)
            closing = asyncio.create_task(persistent.close_idle())
            await asyncio.sleep(0.02)
            assert not closing.done() and borrowed_proc is not None
            assert borrowed_proc.returncode is None
            delayed = await active
            assert delayed[-1].ok is True
            await closing
            assert borrowed_proc.returncode is not None
        finally:
            await persistent.close()
        assert first_proc is not None and first_proc.returncode is not None

    @pytest.mark.parametrize(
        ("case", "expected_ok"),
        [
            ("valid", True),
            ("foreign_identical_initial", False),
            ("wrong_initial_id", False),
            ("valid_steer", True),
            ("wrong_steer_id", False),
        ],
    )
    async def test_rpc_user_start_binds_native_input_id(self, tmp_path, case, expected_ok):
        stub = _stub(
            tmp_path,
            f'''#!{sys.executable}
import sys
sys.path.insert(0, {str(Path(__file__).parent)!r})
from pi_stub_protocol import send, answer_state, next_prompt, read_state, request_response, read_request

import json, sys

answer_state({{"nativeInputProofCapability":"pi-native-input-v1-live-only"}})
prompt = next_prompt()
assert len(prompt["inputId"]) == 32
send({{"type":"response","command":"prompt","id":prompt["id"],"success":True}})
first_id = prompt["inputId"] if {case!r} != "wrong_initial_id" else "0" * 32
initial = {{"type":"message_start","message":{{"role":"user","content":"same text"}}}}
if {case!r} != "foreign_identical_initial": initial["message"]["inputId"] = first_id
send(initial)
if {case!r} in ("valid_steer", "wrong_steer_id"):

    steer = next_prompt()
    assert steer["message"] == "peer" and len(steer["inputId"]) == 32
    send({{"type":"response","command":"prompt","id":steer["id"],"success":True}})
    steer_id = steer["inputId"] if {case!r} == "valid_steer" else "0" * 32
    send({{"type":"message_start","message":{{"role":"user","content":steer["message"],"inputId":steer_id}}}})
send({{"type":"message_end","message":{{
    "role":"assistant","content":[],"stopReason":"stop",
}}}})
send({{"type":"agent_settled"}})
''',
        )
        queue = TurnInbox() if "steer" in case else None
        if queue is not None:
            queue.put_nowait(steer("peer"))
        events = [
            event
            async for event in one_turn_events(
                stub, [], "same text", str(tmp_path), steering_queue=queue
            )
        ]
        assert events[-1].ok is expected_ok
        if not expected_ok:
            assert events[-1].reason_code == (
                "unrecognized_followup_input"
                if case == "wrong_steer_id"
                else "current_prompt_input_missing"
            )

    async def test_native_steer_start_before_prompt_ack_is_authoritative(self, tmp_path):
        stub = _stub(
            tmp_path,
            f'''#!{sys.executable}
import sys
sys.path.insert(0, {str(Path(__file__).parent)!r})
from pi_stub_protocol import send, answer_state, next_prompt, read_state, request_response, read_request, answer_stats

import json, sys

state_data = {{"nativeInputProofCapability":"pi-native-input-v1-live-only"}}
answer_state(state_data)
prompt = next_prompt()
send({{"type":"response","command":"prompt","id":prompt["id"],"success":True}})
send({{"type":"message_start","message":{{"role":"user","content":prompt["message"],
      "inputId":prompt["inputId"]}}}})

steer = next_prompt()
send({{"type":"message_start","message":{{"role":"user","content":steer["message"],
      "inputId":steer["inputId"]}}}})
send({{"type":"response","command":"prompt","id":steer["id"],"success":True}})
send({{"type":"message_end","message":{{
    "role":"assistant","content":[],"stopReason":"stop",
}}}})
send({{"type":"agent_settled"}})
answer_state(state_data)
answer_stats({{"contextUsage":{{}}}})
''',
        )
        queue = TurnInbox()
        queue.put_nowait(steer("channel mention"))
        starts = []

        def native_start(public_id, native_id, text):
            starts.append((public_id, native_id, text))
            return True

        events = [
            event
            async for event in one_turn_events(
                stub,
                [],
                "goal request",
                str(tmp_path),
                steering_queue=queue,
                native_start=native_start,
            )
        ]
        assert events[-1].ok is True, events
        assert [text for _, _, text in starts] == ["goal request", "channel mention"]
        assert len([e for e in events if isinstance(e, ae.InputStarted)]) == 2

    @pytest.mark.parametrize(
        ("decision", "expected_ok"),
        [(RefusedSend(), True), (AuthorityChangedSend(), False)],
    )
    async def test_unsent_followup_goal_defer_or_owner_stop(self, tmp_path, decision, expected_ok):
        checked = tmp_path / "send-boundary-checked"
        stub = _stub(
            tmp_path,
            f'''#!{sys.executable}
import sys
sys.path.insert(0, {str(Path(__file__).parent)!r})
from pi_stub_protocol import send, answer_state, next_prompt, read_state, request_response, read_request, answer_stats

import json, pathlib, sys, time

state_data = {{"nativeInputProofCapability":"pi-native-input-v1-live-only"}}
answer_state(state_data)
prompt = next_prompt()
send({{"type":"response","command":"prompt","id":prompt["id"],"success":True}})
send({{"type":"message_start","message":{{"role":"user","content":prompt["message"],
      "inputId":prompt["inputId"]}}}})
marker = pathlib.Path({str(checked)!r})
for _ in range(200):
    if marker.exists(): break
    time.sleep(0.01)
assert marker.exists()
send({{"type":"message_end","message":{{
    "role":"assistant","content":[],"stopReason":"stop",
}}}})
send({{"type":"agent_settled"}})
answer_state(state_data)
answer_stats({{"contextUsage":{{}}}})
''',
        )
        queue = TurnInbox()
        queue.put_nowait(steer("late direct", "bus-42"))

        @contextmanager
        def send_boundary(public_id, native_id, text):
            if public_id is None:
                yield GrantedSend()
            else:
                assert (public_id, text) == ("bus-42", "late direct")
                checked.write_text("authority checked")
                yield decision

        events = [
            event
            async for event in one_turn_events(
                stub,
                [],
                "set a goal",
                str(tmp_path),
                steering_queue=queue,
                send_boundary=send_boundary,
            )
        ]
        assert events[-1].ok is expected_ok, events
        if expected_ok:
            assert ae.FollowingInputRefused(id="bus-42") in events
        else:
            assert events[-1].reason_code == "input_authority_changed"
        assert queue.empty()


# PR#1 parser assertions retained alongside V3 projection/watchdog coverage.
class TestPrRpcParsing:
    async def test_full_rpc_stream(self, tmp_path):
        """Canned pi rpc stream -> chunk/tool_start/tool_end/done events."""
        rpc_lines = "\n".join(
            [
                '{"type":"response","command":"prompt","id":"agent-comms-prompt","success":true}',
                '{"type":"message_start","message":{"role":"user","content":"task"}}',
                '{"type":"message_update","assistantMessageEvent":{"type":"text_delta","delta":"hel"}}',
                '{"type":"message_update","assistantMessageEvent":{"type":"text_delta","delta":"lo"}}',
                '{"type":"message_update","assistantMessageEvent":'
                '{"type":"thinking_delta","delta":"Checking the workspace"}}',
                '{"type":"tool_execution_start","toolCallId":"t1",'
                '"toolName":"bash","args":{"command":"echo hi"}}',
                '{"type":"tool_execution_update","toolCallId":"t1","toolName":"bash",'
                '"partialResult":{"content":[{"type":"text","text":"running"}]}}',
                '{"type":"tool_execution_end","toolCallId":"t1","toolName":"bash",'
                '"result":{"content":[{"type":"text","text":"hi"}]},"isError":false}',
                '{"type":"message_update",'
                '"assistantMessageEvent":{"type":"text_delta","delta":" done"}}',
                '{"type":"message_end","message":{"role":"assistant","stopReason":"stop"}}',
                '{"type":"agent_settled"}',
            ]
        )
        stub = _replay_stub(tmp_path, rpc_lines)
        events = [e async for e in one_turn_events(stub, [], "task", str(tmp_path))]
        # pi-named stub triggers rpc mode; prompt goes to stdin.
        _assert_full_rpc_stream(events)

    async def test_rpc_model_and_context_metadata(self, tmp_path):
        rpc_lines = "\n".join(
            [
                '{"type":"response","command":"get_state","success":true,"data":'
                '{"model":{"provider":"openrouter","id":"z-ai/glm","contextWindow":1000},'
                '"sessionName":"work","sessionFile":"/tmp/pi-session.jsonl"}}',
                '{"type":"message_update","usage":{"totalTokens":125},'
                '"assistantMessageEvent":{"type":"text_delta","delta":"ok"}}',
                '{"type":"message_end","message":{"role":"assistant","stopReason":"stop"}}',
                '{"type":"agent_settled"}',
                '{"type":"response","command":"get_session_stats","success":true,"data":'
                '{"contextUsage":{"tokens":200,"contextWindow":1000,"percent":20}}}',
            ]
        )
        stub = _replay_stub(tmp_path, rpc_lines)
        events = [e async for e in one_turn_events(stub, [], "t", str(tmp_path))]
        info = [event for event in events if isinstance(event, ae.AgentInfo)]
        assert info[0].model == "openrouter/z-ai/glm"
        assert info[0].session_file == "/tmp/pi-session.jsonl"
        assert len(info) == 1
        context = [event for event in events if isinstance(event, ae.ContextUsageObservation)]
        assert context[-1].context_used == PiCount.from_wire(200)
        assert context[-1].context_size == PiCount.from_wire(1000)

    async def test_rpc_stays_open_for_steered_child_reply(self, tmp_path):
        stub = _stub(
            tmp_path,
            f"""#!/bin/sh
IFS= read -r state
IFS= read -r prompt
printf '%s' "$prompt" | {sys.executable} -c 'import json, sys; print(json.dumps({{"type": "response", "command": "prompt", "id": json.load(sys.stdin)["id"], "success": True}}))'
echo '{{"type":"message_start","message":{{"role":"user","content":"coordinate"}}}}'
echo '{{"type":"agent_settled"}}'
IFS= read -r mode
IFS= read -r steering
echo '{{"type":"message_update","assistantMessageEvent":{{"type":"text_delta","delta":"pong"}}}}'
echo '{{"type":"agent_settled"}}'
IFS= read -r state_after
IFS= read -r stats
echo '{{"type":"response","command":"get_session_stats","success":true,"data":{{"contextUsage":{{}}}}}}'
""",
        )
        queue = TurnInbox()
        finish = asyncio.Event()
        events = []

        async for event in one_turn_events(
            stub,
            [],
            "coordinate",
            str(tmp_path),
            steering_queue=queue,
            finish_event=finish,
        ):
            events.append(event)
            if isinstance(event, ae.StreamSettled):
                if not any(isinstance(item, ae.Chunk) for item in events):
                    queue.put_nowait(steer("[child] ping"))
                else:
                    finish.set()

        assert [event.text for event in events if isinstance(event, ae.Chunk)] == ["pong"]

    @pytest.mark.parametrize("aborted", [False, True])
    async def test_midturn_compaction_forwards_lifecycle_and_clears_stale_usage(
        self, tmp_path, aborted
    ):
        summary = "recap " * 900
        rpc_events = [
            {
                "type": "message_update",
                "message": {"role": "assistant", "content": []},
                "usage": {"totalTokens": 83},
            },
            {"type": "compaction_start", "reason": "threshold"},
            {
                "type": "compaction_end",
                "reason": "threshold",
                "result": None if aborted else {"summary": summary},
                "aborted": aborted,
                "willRetry": False,
            },
            {"type": "message_end", "message": {"role": "assistant", "stopReason": "stop"}},
            {"type": "agent_settled"},
            {
                "type": "response",
                "command": "get_session_stats",
                "success": True,
                "data": {"contextUsage": {}},
            },
        ]
        events = await _rpc_events(tmp_path, rpc_events)
        assert next(
            event
            for event in events
            if isinstance(event, ae.ContextUsageObservation) and event.context_used == PiCount.from_wire(83)
        )
        start = next(event for event in events if isinstance(event, ae.CompactionStart))
        end = next(event for event in events if isinstance(event, ae.CompactionEnd))
        assert start.reason is ThresholdCompactionReason
        assert end.reason is ThresholdCompactionReason
        assert end.aborted is aborted
        assert end.context_used == PiCount.from_wire(None)
        assert end.summary == (None if aborted else summary)
        assert isinstance(events[-2], ae.ContextUsageObservation) and events[-2].context_used == PiCount.from_wire(None)
        assert events[-1].ok is True

    async def test_failed_tool_does_not_fail_recovered_turn(self, tmp_path):
        rpc_lines = "\n".join(
            [
                '{"type":"tool_execution_start","toolCallId":"t1","toolName":"bash","args":{}}',
                '{"type":"tool_execution_end","toolCallId":"t1","toolName":"bash","result":{"content":[{"type":"text","text":"boom"}]},"isError":true}',
                '{"type":"response","command":"prompt","id":"agent-comms-prompt","success":true}',
                '{"type":"message_start","message":{"role":"user","content":"t"}}',
                '{"type":"message_end","message":{"role":"assistant","stopReason":"stop"}}',
                '{"type":"agent_settled"}',
            ]
        )
        stub = _replay_stub(tmp_path, rpc_lines)
        events = [e async for e in one_turn_events(stub, [], "t", str(tmp_path))]
        assert events[-1].ok is True
        tool_ends = [event for event in events if isinstance(event, ae.ToolEnd)]
        assert len(tool_ends) == 1 and tool_ends[0].ok is False

    @pytest.mark.parametrize(
        ("starts", "stops", "expected_reason"),
        [
            ([], ["stop"], "current_prompt_input_missing"),
            (["t"], ["stop"], None),
            (["foreign", "t"], ["stop"], "current_prompt_input_missing"),
            (["t", "foreign"], ["stop"], "unrecognized_followup_input"),
            (["t", "t"], ["stop"], "unrecognized_followup_input"),
            (["t"], [], "assistant_final_stop_missing"),
            (["t"], ["toolUse"], "assistant_final_stop_missing"),
            (["t"], ["error"], "assistant_final_stop_missing"),
            (["t"], ["aborted"], "assistant_final_stop_missing"),
            (["t"], ["toolUse", "stop"], None),
        ],
    )
    async def test_prompt_ack_only_exact_original_start_and_final_stop_prove_success(
        self, tmp_path, starts, stops, expected_reason
    ):
        records = [
            {"type": "response", "command": "prompt", "id": "agent-comms-prompt", "success": True},
            *(
                {"type": "message_start", "message": {"role": "user", "content": value}}
                for value in starts
            ),
            *(
                {"type": "message_end", "message": {"role": "assistant", "stopReason": stop}}
                for stop in stops
            ),
            {"type": "agent_settled"},
        ]
        lines = "\n".join(json.dumps(record) for record in records)
        stub = _replay_stub(tmp_path, lines)
        events = [
            event async for event in one_turn_events(stub, [], "t", str(tmp_path))
        ]
        assert events[-1].ok is (expected_reason is None)
        if expected_reason is not None:
            assert events[-1].reason_code == expected_reason
            assert events[-1].text

    @pytest.mark.parametrize(
        "records",
        [
            [
                {"type": "message_start", "message": {"role": "user", "content": "t"}},
                {
                    "type": "response",
                    "command": "prompt",
                    "id": "agent-comms-prompt",
                    "success": True,
                },
            ],
            [
                {
                    "type": "response",
                    "command": "prompt",
                    "id": "agent-comms-prompt",
                    "success": False,
                },
                {"type": "message_start", "message": {"role": "user", "content": "t"}},
            ],
            [{"type": "message_start", "message": {"role": "user", "content": "t"}}],
        ],
    )
    async def test_no_matching_preflight_before_user_start_fails_closed(self, tmp_path, records):
        lines = "\n".join(
            json.dumps(record)
            for record in [
                *records,
                {"type": "message_end", "message": {"role": "assistant", "stopReason": "stop"}},
                {"type": "agent_settled"},
            ]
        )
        stub = _replay_stub(tmp_path, lines)
        events = [
            event async for event in one_turn_events(stub, [], "t", str(tmp_path))
        ]
        assert events[-1].ok is False
        assert events[-1].reason_code == "current_prompt_input_missing"

    @pytest.mark.parametrize("managed_finish", [False, True])
    async def test_handled_ack_without_user_start_times_out_without_replay(
        self, tmp_path, monkeypatch, managed_finish
    ):
        monkeypatch.setattr(backend, "PROMPT_START_TIMEOUT_SECONDS", 0.5)
        calls = tmp_path / "prompt-calls"
        stub = _stub(
            tmp_path,
            f"#!{sys.executable}\n"
            + f"""
import json, sys
sys.path.insert(0, {str(Path(__file__).parent)!r})
from pi_stub_protocol import answer_state, next_prompt, read_request, request_response, send
answer_state({{"nativeInputProofCapability": {CAPABILITY!r}}})
prompt = next_prompt()
with open({str(calls)!r}, "a") as handle:
    handle.write("one\\n")
    handle.flush()
send(request_response(prompt, {{}}))
abort = read_request()
assert abort["type"] == "abort"
send(request_response(abort, {{}}))
""",
        )
        finish = asyncio.Event() if managed_finish else None

        async def collect():
            return [
                event
                async for event in one_turn_events(
                    stub, [], "t", str(tmp_path), finish_event=finish
                )
            ]

        events = await asyncio.wait_for(collect(), timeout=3)
        assert calls.read_text().splitlines() == ["one"]
        assert [event for event in events if isinstance(event, ae.Done)] == [events[-1]]
        assert events[-1].ok is False
        assert events[-1].reason_code == "current_prompt_input_missing"

    @pytest.mark.parametrize(
        "later_event",
        [
            {"type": "message_start", "message": {"role": "assistant"}},
            {"type": "agent_start"},
            {"type": "agent_end", "willRetry": True},
            {"type": "auto_retry_start"},
            {"type": "compaction_end", "willRetry": True},
        ],
    )
    async def test_prior_final_cannot_authorize_incomplete_later_run(self, tmp_path, later_event):
        records = [
            {"type": "response", "command": "prompt", "id": "agent-comms-prompt", "success": True},
            {"type": "message_start", "message": {"role": "user", "content": "t"}},
            {"type": "message_end", "message": {"role": "assistant", "stopReason": "stop"}},
            later_event,
            {"type": "agent_settled"},
        ]
        lines = "\n".join(json.dumps(record) for record in records)
        stub = _replay_stub(tmp_path, lines)
        events = [
            event async for event in one_turn_events(stub, [], "t", str(tmp_path))
        ]
        assert events[-1].ok is False
        assert events[-1].reason_code == "assistant_final_stop_missing"
