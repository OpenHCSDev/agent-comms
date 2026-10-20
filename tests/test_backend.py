"""Native streaming: external RPC parsing and failure handling."""

import asyncio
import json
import sys
from pathlib import Path
from uuid import uuid4

import pytest

import agent_comms.agent_events as ae
import agent_comms.backend as backend
from agent_comms.agent_backend import InputContent, InputId, InputRequest, WhenBusy
from agent_comms.native_pi import CAPABILITY
from agent_comms.pi_rpc import PiRpcChannel
from agent_comms.turn_phase import (
    ModelWaitPhase,
    SettlingStatsPhase,
    ToolRunningPhase,
)
from agent_comms.turn_watchdog import ModelWait
from pi_session_turn import one_turn_events


def steer(text: str, input_id: str | None = None) -> InputRequest:
    """A queued steering input as Core hands it to the running turn."""
    return InputRequest(
        input_id=InputId(input_id or f"agent-comms-steer-{uuid4().hex}"),
        content=InputContent(text=text),
        when_busy=WhenBusy.STEER,
    )


pytestmark = [
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
# One Pi session answers every state query from the same session state.
state_data = {{"nativeInputProofCapability": CAPABILITY}}
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
                "data": state_data if request.get("type") == "get_state" else {{}},
            }}), flush=True)
    if command == "get_state":
        record.setdefault("data", {{}})["nativeInputProofCapability"] = CAPABILITY
        state_data = record["data"]
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
































# Native per-input proof tests retained from the PR#1 parent.
class TestNativeInputBinding:
    def test_only_original_response_can_promote_pending_attestation(self):
        from agent_comms.native_attestation import AttestationError, PendingAttestation
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
            pending.accept(response(pending.request.require_id(), 'unsupported'))
        assert pending.accept(response(pending.request.require_id())).observed
















