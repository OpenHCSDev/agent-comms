"""Streaming backend: rpc parsing, text fallback, failure handling."""

import asyncio
import json
import os
import signal
import sys
from contextlib import suppress
from pathlib import Path

import pytest

from agent_comms import backend

pytestmark = pytest.mark.skipif(
    sys.platform == "win32", reason="backend tests exec shell-script stubs; POSIX only"
)


def _stub(tmp_path: Path, body: str, name: str = "pi-stub") -> str:
    stub = tmp_path / name
    stub.write_text(body)
    stub.chmod(0o755)
    return str(stub)


def _capture_unexpected_rpc_exception(monkeypatch):
    """Temporarily expose an early-exit CI race without changing runtime diagnostics."""
    import traceback

    failures = []
    implementation = backend._stream_agent_events_impl

    async def traced(*args, **kwargs):
        try:
            async for event in implementation(*args, **kwargs):
                yield event
        except Exception:
            failures.append(traceback.format_exc())
            raise

    monkeypatch.setattr(backend, "_stream_agent_events_impl", traced)
    return failures


class TestRpcParsing:
    @pytest.fixture(autouse=True)
    def legacy_stock_pi_fixture(self, monkeypatch):
        """Old event-shape fixtures cover projection, not input provenance."""
        original = backend.stream_agent_events

        async def legacy(*args, **kwargs):
            kwargs.setdefault("require_input_id", False)
            async for event in original(*args, **kwargs):
                yield event

        monkeypatch.setattr(backend, "stream_agent_events", legacy)

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
        stub = _stub(tmp_path, f"#!/bin/sh\ntrue\ncat <<'EOF'\n{rpc_lines}\nEOF\n")
        events = [e async for e in backend.stream_agent_events(stub, [], "task", str(tmp_path))]
        # pi-named stub triggers rpc mode; prompt goes to stdin.
        types = [e["type"] for e in events]
        assert types == [
            "chunk",
            "chunk",
            "thinking",
            "tool_start",
            "tool_progress",
            "tool_end",
            "chunk",
            "settled",
            "done",
        ]
        assert events[2]["text"] == "Checking the workspace"
        tool_start = events[3]
        assert tool_start["name"] == "bash" and tool_start["id"] == "t1"
        assert "echo hi" in tool_start["title"]
        assert events[4]["output"] == "running"
        tool_end = events[5]
        assert tool_end["ok"] is True and "hi" in tool_end["output"]
        assert events[-1]["text"] == "hello done" and events[-1]["ok"] is True

    async def test_rpc_model_and_context_metadata(self, tmp_path):
        rpc_lines = "\n".join(
            [
                '{"type":"response","command":"prompt","id":"agent-comms-prompt","success":true}',
                '{"type":"message_start","message":{"role":"user","content":"t"}}',
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
        stub = _stub(tmp_path, f"#!/bin/sh\ncat <<'EOF'\n{rpc_lines}\nEOF\n")
        events = [e async for e in backend.stream_agent_events(stub, [], "t", str(tmp_path))]
        info = [event for event in events if event["type"] == "agent_info"]
        assert info[0]["model"] == "openrouter/z-ai/glm"
        assert info[0]["session_file"] == "/tmp/pi-session.jsonl"
        assert info[-1]["context_used"] == 200
        assert info[-1]["context_size"] == 1000

    async def test_rpc_resumes_session_and_forwards_live_prompts(self, tmp_path):
        args_path = tmp_path / "args"
        steering_path = tmp_path / "steering"
        session_path = tmp_path / "session.jsonl"
        stub = _stub(
            tmp_path,
            f"""#!/bin/sh
printf '%s' "$*" > '{args_path}'
IFS= read -r state
IFS= read -r prompt
echo '{{"type":"response","command":"prompt","id":"agent-comms-prompt","success":true}}'
echo '{{"type":"message_start","message":{{"role":"user","content":"parent task"}}}}'
IFS= read -r steering
printf '%s' "$steering" > '{steering_path}'
cat <<'EOF'
{{"type":"response","command":"get_state","success":true,"data":{{"sessionFile":"{session_path}"}}}}
{{"type":"agent_settled"}}
{{"type":"response","command":"get_session_stats","success":true,"data":{{"contextUsage":{{}}}}}}
EOF
""",
        )
        queue: asyncio.Queue[str] = asyncio.Queue()
        queue.put_nowait("[peer] ping")

        events = [
            event
            async for event in backend.stream_agent_events(
                stub,
                [],
                "parent task",
                str(tmp_path),
                session_file=str(session_path),
                steering_queue=queue,
            )
        ]

        assert f"--session {session_path}" in args_path.read_text()
        assert '"message": "[agent-comms input-id: agent-comms-steer-' in steering_path.read_text()
        assert ']\\n[peer] ping"' in steering_path.read_text()
        assert '"streamingBehavior": "steer"' in steering_path.read_text()
        assert any(
            event.get("session_file") == str(session_path)
            for event in events
            if event["type"] == "agent_info"
        )

    async def test_rpc_stays_open_for_steered_child_reply(self, tmp_path):
        stub = _stub(
            tmp_path,
            """#!/bin/sh
IFS= read -r state
IFS= read -r prompt
echo '{"type":"response","command":"prompt","id":"agent-comms-prompt","success":true}'
echo '{"type":"message_start","message":{"role":"user","content":"coordinate"}}'
echo '{"type":"agent_settled"}'
IFS= read -r steering
echo '{"type":"message_update","assistantMessageEvent":{"type":"text_delta","delta":"pong"}}'
echo '{"type":"agent_settled"}'
IFS= read -r state_after
IFS= read -r stats
echo '{"type":"response","command":"get_session_stats","success":true,"data":{"contextUsage":{}}}'
""",
        )
        queue: asyncio.Queue[str] = asyncio.Queue()
        finish = asyncio.Event()
        events = []

        async for event in backend.stream_agent_events(
            stub,
            [],
            "coordinate",
            str(tmp_path),
            steering_queue=queue,
            finish_event=finish,
        ):
            events.append(event)
            if event["type"] == "settled":
                if not any(item["type"] == "chunk" for item in events):
                    queue.put_nowait("[child] ping")
                else:
                    finish.set()

        assert [event["text"] for event in events if event["type"] == "chunk"] == ["pong"]

    @pytest.mark.parametrize("aborted", [False, True])
    async def test_midturn_compaction_forwards_lifecycle_and_clears_stale_usage(
        self, tmp_path, aborted
    ):
        summary = "recap " * 900
        rpc_events = [
            {"type": "response", "command": "prompt", "id": "agent-comms-prompt", "success": True},
            {"type": "message_start", "message": {"role": "user", "content": "work"}},
            {"type": "message_update", "usage": {"totalTokens": 83}},
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
        stub = _stub(
            tmp_path,
            f"#!{sys.executable}\nimport json, sys\n"
            "sys.stdin.readline(); sys.stdin.readline()\n"
            f"for event in {rpc_events!r}: print(json.dumps(event), flush=True)\n",
        )
        events = [
            event async for event in backend.stream_agent_events(stub, [], "work", str(tmp_path))
        ]
        assert next(
            event
            for event in events
            if event["type"] == "agent_info" and event["context_used"] == 83
        )
        start = next(event for event in events if event["type"] == "compaction_start")
        end = next(event for event in events if event["type"] == "compaction_end")
        assert start["reason"] == "threshold"
        assert end["reason"] == "threshold"
        assert end["aborted"] is aborted
        assert end["context_used"] is None
        assert end["summary"] == (None if aborted else summary.strip()[:4096])
        assert events[-2]["type"] == "agent_info" and events[-2]["context_used"] is None
        assert events[-1]["ok"] is True

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
        stub = _stub(tmp_path, f"#!/bin/sh\ncat <<'EOF'\n{rpc_lines}\nEOF\n")
        events = [e async for e in backend.stream_agent_events(stub, [], "t", str(tmp_path))]
        assert events[-1]["ok"] is True
        tool_end = events[1]
        assert tool_end["ok"] is False

    @pytest.mark.parametrize(
        ("starts", "stops", "expected_reason"),
        [
            ([], ["stop"], "current_prompt_input_missing"),
            (["t"], ["stop"], None),
            (["foreign", "t"], ["stop"], "current_prompt_input_missing"),
            (["t", "foreign"], ["stop"], "current_prompt_input_missing"),
            (["t", "t"], ["stop"], "current_prompt_input_missing"),
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
        stub = _stub(tmp_path, f"#!/bin/sh\ncat <<'EOF'\n{lines}\nEOF\n")
        events = [
            event async for event in backend.stream_agent_events(stub, [], "t", str(tmp_path))
        ]
        assert events[-1]["ok"] is (expected_reason is None)
        if expected_reason is not None:
            assert events[-1]["reason_code"] == expected_reason
            assert events[-1]["text"]

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
    async def test_no_matching_preflight_before_user_start_fails_closed(
        self, tmp_path, records, monkeypatch
    ):
        exceptions = _capture_unexpected_rpc_exception(monkeypatch)
        lines = "\n".join(
            json.dumps(record)
            for record in [
                *records,
                {"type": "message_end", "message": {"role": "assistant", "stopReason": "stop"}},
                {"type": "agent_settled"},
            ]
        )
        stub = _stub(tmp_path, f"#!/bin/sh\ncat <<'EOF'\n{lines}\nEOF\n")
        events = [
            event async for event in backend.stream_agent_events(stub, [], "t", str(tmp_path))
        ]
        assert events[-1]["ok"] is False
        assert events[-1]["reason_code"] == "current_prompt_input_missing", "\n".join(exceptions)

    @pytest.mark.parametrize(
        ("steer_accepted", "foreign_first", "expected_ok"),
        [(True, False, True), (False, False, False), (True, True, False)],
    )
    async def test_marked_steer_start_requires_accepted_matching_input(
        self, tmp_path, steer_accepted, foreign_first, expected_ok
    ):
        stub = _stub(
            tmp_path,
            f"#!{sys.executable}\n" + f"""
import json, sys
send = lambda value: print(json.dumps(value), flush=True)
state = json.loads(sys.stdin.readline())
prompt = json.loads(sys.stdin.readline())
assert state["type"] == "get_state" and prompt["message"] == "t"
send({{"type": "response", "command": "prompt", "id": prompt["id"], "success": True}})
if {foreign_first}:
    send({{"type": "message_start", "message": {{"role": "user", "content": "peer"}}}})
    send({{"type": "message_start", "message": {{"role": "user", "content": "t"}}}})
else:
    send({{"type": "message_start", "message": {{"role": "user", "content": "t"}}}})
    steer = json.loads(sys.stdin.readline())
    assert steer["message"] == f'[agent-comms input-id: {{steer["id"]}}]\\npeer'
    assert steer["streamingBehavior"] == "steer"
    send({{"type": "response", "command": "prompt", "id": steer["id"],
          "success": {steer_accepted}}})
    send({{"type": "message_start", "message": {{"role": "user", "content": steer["message"]}}}})
send({{"type": "message_end", "message": {{"role": "assistant", "stopReason": "stop"}}}})
send({{"type": "agent_settled"}})
""",
        )
        queue: asyncio.Queue[str] = asyncio.Queue()
        queue.put_nowait("peer")
        events = [
            event
            async for event in backend.stream_agent_events(
                stub, [], "t", str(tmp_path), steering_queue=queue
            )
        ]
        assert events[-1]["ok"] is expected_ok
        if not expected_ok:
            assert events[-1]["reason_code"] == "current_prompt_input_missing"

    async def test_identified_steer_ack_cannot_claim_unrelated_identical_user_start(self, tmp_path):
        """The two possible sources have identical stock Pi RPC event shapes."""
        stub = _stub(
            tmp_path,
            f"#!{sys.executable}\n" + """
import json, sys
send = lambda value: print(json.dumps(value), flush=True)
sys.stdin.readline()
prompt = json.loads(sys.stdin.readline())
send({"type": "response", "command": "prompt", "id": prompt["id"], "success": True})
send({"type": "message_start", "message": {"role": "user", "content": "t"}})
steer = json.loads(sys.stdin.readline())
assert steer["message"] == f'[agent-comms input-id: {steer["id"]}]\\npeer'
# Pi's ACK can mean queued or handled by an extension; no user start is promised.
send({"type": "response", "command": "prompt", "id": steer["id"], "success": True})
# Another independent user event has the *same* literal text as the queued steer.
send({"type": "message_start", "message": {"role": "user", "content": "peer"}})
send({"type": "message_end", "message": {"role": "assistant", "stopReason": "stop"}})
send({"type": "agent_settled"})
""",
        )
        queue: asyncio.Queue[str] = asyncio.Queue()
        queue.put_nowait("peer")
        events = [
            event
            async for event in backend.stream_agent_events(
                stub, [], "t", str(tmp_path), steering_queue=queue
            )
        ]
        assert events[-1]["ok"] is False
        assert events[-1]["reason_code"] == "current_prompt_input_missing"

    @pytest.mark.parametrize("managed_finish", [False, True])
    async def test_handled_ack_without_user_start_times_out_without_replay(
        self, tmp_path, monkeypatch, managed_finish
    ):
        monkeypatch.setattr(backend, "PROMPT_START_TIMEOUT_SECONDS", 0.5)
        calls = tmp_path / "prompt-calls"
        stub = _stub(
            tmp_path,
            f"#!{sys.executable}\n" + f"""
import json, sys, time
sys.stdin.readline()
prompt = json.loads(sys.stdin.readline())
with open({str(calls)!r}, "a") as handle:
    handle.write("one\\n")
    handle.flush()
print(json.dumps({{"type": "response", "command": "prompt",
                  "id": prompt["id"], "success": True}}), flush=True)
time.sleep(30)
""",
        )
        finish = asyncio.Event() if managed_finish else None

        async def collect():
            return [
                event
                async for event in backend.stream_agent_events(
                    stub, [], "t", str(tmp_path), finish_event=finish
                )
            ]

        events = await asyncio.wait_for(collect(), timeout=3)
        assert calls.read_text().splitlines() == ["one"]
        assert [event for event in events if event["type"] == "done"] == [events[-1]]
        assert events[-1]["ok"] is False
        assert events[-1]["reason_code"] == "current_prompt_input_missing"

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
        stub = _stub(tmp_path, f"#!/bin/sh\ncat <<'EOF'\n{lines}\nEOF\n")
        events = [
            event async for event in backend.stream_agent_events(stub, [], "t", str(tmp_path))
        ]
        assert events[-1]["ok"] is False
        assert events[-1]["reason_code"] == "assistant_final_stop_missing"


class TestNativeInputBinding:
    async def test_malformed_native_user_content_fails_typed_and_reaps_live_child(self, tmp_path):
        pid_file = tmp_path / "pi.pid"
        stub = _stub(
            tmp_path,
            f"#!{sys.executable}\n" + f"""
import json, os, signal, sys, time
state = json.loads(sys.stdin.readline())
print(json.dumps({{"type":"response","command":"get_state","id":state["id"],
                  "success":True,"data":{{"nativeInputProofCapability":
                  "pi-native-input-v1-live-only"}}}}), flush=True)
prompt = json.loads(sys.stdin.readline())
print(json.dumps({{"type":"response","command":"prompt","id":prompt["id"],
                  "success":True}}), flush=True)
print(json.dumps({{"type":"message_start","message":{{"role":"user",
                  "inputId":prompt["inputId"],"content":[{{"type":"text","text":None}}]}}}}),
      flush=True)
open({str(pid_file)!r}, 'w').write(str(os.getpid()))
signal.signal(signal.SIGTERM, lambda *_: None)
while True: time.sleep(0.1)
""",
        )
        events = [
            event async for event in backend.stream_agent_events(stub, [], "work", str(tmp_path))
        ]
        assert [event for event in events if event["type"] == "done"] == [events[-1]]
        assert events[-1]["ok"] is False
        assert events[-1]["reason_code"] == "pi_invalid_rpc_event"
        assert "NoneType" not in events[-1]["text"]
        assert pid_file.exists()
        with pytest.raises(ProcessLookupError):
            os.kill(int(pid_file.read_text()), 0)
        assert not backend._ACTIVE_PROCESSES
        assert not backend._ACTIVE_STDERR_TASKS

    @pytest.mark.parametrize("exit_mode", ["malformed", "early_close"])
    async def test_steering_forwarder_is_reaped_on_invalid_rpc_or_early_close(
        self, tmp_path, exit_mode
    ):
        pid_file = tmp_path / "pi.pid"
        stub = _stub(
            tmp_path,
            f"#!{sys.executable}\n" + f"""
import json, os, signal, sys, time
state = json.loads(sys.stdin.readline())
print(json.dumps({{"type":"response","command":"get_state","id":state["id"],
                  "success":True,"data":{{"nativeInputProofCapability":
                  "pi-native-input-v1-live-only"}}}}), flush=True)
prompt = json.loads(sys.stdin.readline())
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
""",
        )
        queue: asyncio.Queue[str] = asyncio.Queue()
        stream = backend.stream_agent_events(stub, [], "work", str(tmp_path), steering_queue=queue)
        try:
            if exit_mode == "malformed":
                async with asyncio.timeout(4):
                    events = [event async for event in stream]
                assert events[-1]["reason_code"] == "pi_invalid_rpc_event"
            else:
                async with asyncio.timeout(4):
                    while (await stream.__anext__())["type"] != "chunk":
                        pass
                    await stream.aclose()
            assert pid_file.exists()
            with pytest.raises(ProcessLookupError):
                os.kill(int(pid_file.read_text()), 0)
            assert not backend._ACTIVE_STEERING_TASKS
            assert not backend._ACTIVE_PROCESSES
            assert not backend._ACTIVE_STDERR_TASKS
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
        self, tmp_path, preflight, monkeypatch
    ):
        exceptions = _capture_unexpected_rpc_exception(monkeypatch)
        stub = _stub(
            tmp_path,
            f"#!{sys.executable}\n" + f"""
import json, sys
state = json.loads(sys.stdin.readline())
assert state["type"] == "get_state"
case = {preflight!r}
if case != "eof":
    data = "not-a-dict" if case == "invalid_data" else {{
        "nativeInputProofCapability":"pi-native-input-v1-live-only"
    }}
    print(json.dumps({{"type":"response","command":"get_state",
                      "id":state["id"] if case != "wrong_id" else "foreign",
                      "success":True,"data":data}}), flush=True)
""",
        )
        events = [
            event async for event in backend.stream_agent_events(stub, [], "work", str(tmp_path))
        ]
        assert [event for event in events if event["type"] == "done"] == [events[-1]]
        assert events[-1]["ok"] is False
        assert events[-1]["reason_code"] == "pi_input_id_unavailable", "\n".join(exceptions)
        assert "preflight" in events[-1]["text"]

    @pytest.mark.parametrize(
        ("case", "expected_reason"),
        [
            ("accepted_without_start", "queued_input_start_missing"),
            ("accepted_then_started", None),
            ("rejected_without_start", "queued_input_start_missing"),
            ("unacknowledged", "queued_input_start_missing"),
            ("accepted_then_duplicate", "current_prompt_input_missing"),
        ],
    )
    async def test_queued_steer_needs_its_own_user_start_before_success(
        self, tmp_path, case, expected_reason
    ):
        """An original-only final/stats cannot settle an ACP-acknowledged queued input."""
        stub = _stub(
            tmp_path,
            f"#!{sys.executable}\n" + f"""
import json, sys
send = lambda event: print(json.dumps(event), flush=True)
state = json.loads(sys.stdin.readline())
send({{"type":"response", "command":"get_state", "id":state["id"],
       "success":True, "data":{{"nativeInputProofCapability":"pi-native-input-v1-live-only"}}}})
prompt = json.loads(sys.stdin.readline())
send({{"type":"response", "command":"prompt", "id":prompt["id"], "success":True}})
send({{"type":"message_start", "message":{{"role":"user", "content":prompt["message"],
       "inputId":prompt["inputId"]}}}})
steer = json.loads(sys.stdin.readline())
assert steer["streamingBehavior"] == "steer" and steer["message"] == "inbox update"
case = {case!r}
if case != "unacknowledged":
    send({{"type":"response", "command":"prompt", "id":steer["id"],
           "success":case != "rejected_without_start"}})
if case in ("accepted_then_started", "accepted_then_duplicate"):
    event = {{"type":"message_start", "message":{{"role":"user",
             "content":steer["message"], "inputId":steer["inputId"]}}}}
    send(event)
    if case == "accepted_then_duplicate": send(event)
send({{"type":"message_update", "assistantMessageEvent":
       {{"type":"text_delta", "delta":"original only"}}}})
send({{"type":"message_end", "message":{{"role":"assistant", "stopReason":"stop"}}}})
send({{"type":"agent_settled"}})
sys.stdin.readline()  # postturn get_state
sys.stdin.readline()  # get_session_stats
send({{"type":"response", "command":"get_session_stats", "success":True,
       "data":{{"contextUsage":{{"tokens":33}}}}}})
""",
        )
        queue: asyncio.Queue[str] = asyncio.Queue()
        queue.put_nowait("inbox update")
        async with asyncio.timeout(4):
            events = [
                event
                async for event in backend.stream_agent_events(
                    stub, [], "initial turn", str(tmp_path), steering_queue=queue
                )
            ]
        assert [event for event in events if event["type"] == "done"] == [events[-1]]
        assert events[-1]["ok"] is (expected_reason is None)
        if expected_reason is None:
            assert events[-1]["text"] == "original only"
        else:
            assert events[-1]["reason_code"] == expected_reason
            assert "original only" not in events[-1]["text"]
        assert not backend._ACTIVE_PROCESSES
        assert not backend._ACTIVE_STEERING_TASKS

    async def test_inbox_queued_at_settled_but_not_dispatched_is_not_success(self, tmp_path):
        stub = _stub(
            tmp_path,
            f"#!{sys.executable}\n" + """
import json, sys
send = lambda event: print(json.dumps(event), flush=True)
state = json.loads(sys.stdin.readline())
send({"type":"response","command":"get_state","id":state["id"],
      "success":True,"data":{"nativeInputProofCapability":"pi-native-input-v1-live-only"}})
prompt = json.loads(sys.stdin.readline())
send({"type":"response","command":"prompt","id":prompt["id"],"success":True})
send({"type":"message_start","message":{"role":"user", "content":prompt["message"],
      "inputId":prompt["inputId"]}})
send({"type":"message_end","message":{"role":"assistant", "stopReason":"stop"}})
send({"type":"agent_settled"})
for line in sys.stdin:
    request = json.loads(line)
    if request["type"] == "get_session_stats":
        send({"type":"response","command":"get_session_stats","success":True,"data":{}})
        break
""",
        )
        queue: asyncio.Queue[str] = asyncio.Queue()
        finish = asyncio.Event()
        events = []
        async with asyncio.timeout(4):
            async for event in backend.stream_agent_events(
                stub, [], "initial turn", str(tmp_path), steering_queue=queue, finish_event=finish
            ):
                events.append(event)
                if event["type"] == "settled":
                    queue.put_nowait("late inbox update")
                    finish.set()
        assert events[-1]["ok"] is False
        assert events[-1]["reason_code"] == "queued_input_start_missing"
        assert [event for event in events if event["type"] == "done"] == [events[-1]]

    async def test_stock_pi_capability_preflight_sends_no_prompt(self, tmp_path):
        received = tmp_path / "received-prompt"
        stub = _stub(
            tmp_path,
            f"#!{sys.executable}\n" + f"""
import json, select, sys
state = json.loads(sys.stdin.readline())
assert state["type"] == "get_state"
print(json.dumps({{"type":"response","command":"get_state","id":state["id"],
                  "success":True,"data":{{}}}}), flush=True)
if select.select([sys.stdin], [], [], 0.2)[0]:
    line = sys.stdin.readline()
    if line: open({str(received)!r}, "w").write(line)
""",
        )
        events = [
            event async for event in backend.stream_agent_events(stub, [], "work", str(tmp_path))
        ]
        assert events[-1]["ok"] is False
        assert "native input-ID capability" in events[-1]["text"]
        assert events[-1]["reason_code"] == "pi_input_id_unavailable"
        assert not received.exists()

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
            f"#!{sys.executable}\n" + f"""
import json, sys
send = lambda event: print(json.dumps(event), flush=True)
state = json.loads(sys.stdin.readline())
assert state["type"] == "get_state"
send({{"type":"response","command":"get_state","id":state["id"],
       "success":True,"data":{{"nativeInputProofCapability":"pi-native-input-v1-live-only"}}}})
prompt = json.loads(sys.stdin.readline())
assert len(prompt["inputId"]) == 32
send({{"type":"response","command":"prompt","id":prompt["id"],"success":True}})
first_id = prompt["inputId"] if {case!r} != "wrong_initial_id" else "0" * 32
initial = {{"type":"message_start","message":{{"role":"user","content":"same text"}}}}
if {case!r} != "foreign_identical_initial": initial["message"]["inputId"] = first_id
send(initial)
if {case!r} in ("valid_steer", "wrong_steer_id"):
    steer = json.loads(sys.stdin.readline())
    assert steer["message"] == "peer" and len(steer["inputId"]) == 32
    send({{"type":"response","command":"prompt","id":steer["id"],"success":True}})
    steer_id = steer["inputId"] if {case!r} == "valid_steer" else "0" * 32
    send({{"type":"message_start","message":{{"role":"user","content":steer["message"],"inputId":steer_id}}}})
send({{"type":"message_end","message":{{"role":"assistant","stopReason":"stop"}}}})
send({{"type":"agent_settled"}})
""",
        )
        queue = asyncio.Queue() if "steer" in case else None
        if queue is not None:
            queue.put_nowait("peer")
        events = [
            event
            async for event in backend.stream_agent_events(
                stub, [], "same text", str(tmp_path), steering_queue=queue
            )
        ]
        assert events[-1]["ok"] is expected_ok
        if not expected_ok:
            assert events[-1]["reason_code"] == "current_prompt_input_missing"


class TestTextFallback:
    async def test_non_pi_backend_streams_raw_output(self, tmp_path):
        stub = _stub(tmp_path, "#!/bin/sh\necho plain reply\n", name="echo-stub")
        events = [e async for e in backend.stream_agent_events(stub, [], "task", str(tmp_path))]
        assert events[-1]["type"] == "done"
        assert "plain reply" in events[-1]["text"]
        assert any(e["type"] == "chunk" for e in events)

    async def test_missing_backend_yields_done_not_ok(self, tmp_path):
        events = [
            e
            async for e in backend.stream_agent_events(
                "definitely-not-real-bin-xyz", [], "t", str(tmp_path)
            )
        ]
        assert len(events) == 1
        assert events[0]["type"] == "done" and events[0]["ok"] is False
        assert "not found" in events[0]["text"]

    async def test_nonzero_exit_marks_not_ok(self, tmp_path):
        stub = _stub(tmp_path, "#!/bin/sh\necho partial\nexit 3\n", name="echo-stub")
        events = [e async for e in backend.stream_agent_events(stub, [], "t", str(tmp_path))]
        assert events[-1]["ok"] is False


class TestRpcArgs:
    def test_pi_gets_rpc_mode(self):
        assert backend.rpc_args_for("pi", ["--print"]) == ["--print", "--mode", "rpc"]
        assert backend.rpc_args_for("/usr/local/bin/pi", []) == ["--mode", "rpc"]

    def test_other_backends_stay_text(self):
        assert backend.rpc_args_for("codex", ["exec"]) is None

    def test_tool_kind_mapping(self):
        assert backend.tool_kind("bash") == "execute"
        assert backend.tool_kind("read") == "read"
        assert backend.tool_kind("weird") == "other"
