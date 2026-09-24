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


async def _rpc_events(
    tmp_path: Path, records: list[dict], *, current_input: bool = True
) -> list[dict]:
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
    stub = _stub(tmp_path, f"#!/bin/sh\ncat <<'EOF'\n{lines}\nEOF\n")
    return [event async for event in backend.stream_agent_events(stub, [], "t", str(tmp_path))]


class TestRpcParsing:
    @pytest.fixture(autouse=True)
    def legacy_stock_pi_fixture(self, monkeypatch):
        """Historical projection fixtures are not native input-ID proofs."""
        original = backend.stream_agent_events

        async def legacy(*args, **kwargs):
            kwargs.setdefault("require_input_id", False)
            async for event in original(*args, **kwargs):
                yield event

        monkeypatch.setattr(backend, "stream_agent_events", legacy)

    def test_default_watchdog_yields_to_pi_provider_idle_authority(self):
        assert backend.MODEL_WAIT_TIMEOUT_SECONDS > backend._PI_0_85_1_PROVIDER_IDLE_TIMEOUT_SECONDS

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
        events = await _rpc_events(
            tmp_path,
            [
                {
                    "type": "response",
                    "command": "prompt",
                    "id": "agent-comms-prompt",
                    "success": True,
                },
                *(
                    {"type": "message_end", "message": {"role": "assistant", "stopReason": stop}}
                    for stop in assistant_stops
                ),
                {"type": "agent_settled"},
                {
                    "type": "response",
                    "command": "get_session_stats",
                    "success": True,
                    "data": {"contextUsage": {}},
                },
            ],
        )
        assert events[-1]["ok"] is successful
        if not successful and assistant_stops[-1:] not in (["error"], ["aborted"]):
            assert events[-1]["reason_code"] == "assistant_final_stop_missing"

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
        assert events[-1]["ok"] is False
        expected = (
            "current_prompt_input_missing"
            if subsequent_event.get("message", {}).get("role") == "user"
            else "assistant_final_stop_missing"
        )
        assert events[-1]["reason_code"] == expected

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
        assert events[-1]["ok"] is False
        assert events[-1]["reason_code"] == (expected_reason or "assistant_final_stop_missing")

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
                "current_prompt_input_missing",
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
        assert events[-1]["ok"] is (reason_code is None)
        if reason_code is not None:
            assert events[-1]["reason_code"] == reason_code

    @pytest.mark.parametrize("managed_finish", [False, True])
    async def test_large_end_of_turn_record_does_not_fail_completed_reply(
        self, tmp_path, managed_finish
    ):
        stub = _stub(
            tmp_path,
            f"#!{sys.executable}\n" + """
import json, sys
def emit(value):
    print(json.dumps(value), flush=True)
for line in sys.stdin:
    command = json.loads(line)["type"]
    if command == "prompt":
        emit({"type": "response", "command": "prompt", "id": "agent-comms-prompt", "success": True})
        emit({"type": "message_start", "message": {"role": "user", "content": "task"}})
        emit({"type": "message_update", "assistantMessageEvent": {
            "type": "text_delta", "delta": "completed reply"}})
        # Pi includes the turn's messages in one large agent_end record.
        emit({"type": "message_end", "message": {"role": "assistant", "stopReason": "stop"}})
        emit({"type": "agent_end", "messages": [{"role": "assistant", "content": "x" * 1048576}]})
        emit({"type": "agent_settled"})
    elif command == "get_session_stats":
        emit({"type": "response", "command": command, "success": True,
            "data": {"contextUsage": {"tokens": 64330, "contextWindow": 1048576}}})
        break
""",
        )
        finish = asyncio.Event() if managed_finish else None
        events = []
        async for event in backend.stream_agent_events(
            stub, [], "task", str(tmp_path), finish_event=finish
        ):
            events.append(event)
            if event["type"] == "settled" and finish is not None:
                finish.set()
        assert [event["text"] for event in events if event["type"] == "chunk"] == [
            "completed reply"
        ]
        assert any(event.get("context_used") == 64330 for event in events)
        assert events[-1] == {"type": "done", "ok": True, "text": "completed reply"}

    async def test_partial_large_record_survives_cancelled_read(self):
        stream = asyncio.StreamReader(limit=8)
        reader = backend._JsonLineReader(stream)
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
            f"#!{sys.executable}\n"
            + "import os, pathlib, time\n"
            + f"pathlib.Path({str(pid_file)!r}).write_text(str(os.getpid()))\n"
            + "print('[]', flush=True)\ntime.sleep(60)\n",
        )
        owner = asyncio.current_task()
        events = [
            event
            async for event in backend.stream_agent_events(
                stub, [], "task", str(tmp_path), steering_queue=asyncio.Queue()
            )
        ]
        assert events[-1]["ok"] is False
        assert events[-1]["reason_code"] == "pi_invalid_rpc_event"
        assert pid_file.exists()
        with pytest.raises(ProcessLookupError):
            os.kill(int(pid_file.read_text()), 0)
        assert owner not in backend._ACTIVE_PROCESSES
        assert owner not in backend._ACTIVE_STEERING

    async def test_caller_cancellation_reaps_backend_and_steering_task(self, tmp_path):
        stub = _stub(
            tmp_path,
            f"#!{sys.executable}\n" + "import time\ntime.sleep(60)\n",
        )
        queue: asyncio.Queue[str] = asyncio.Queue()
        queue.put_nowait("[peer] pending")

        async def consume() -> None:
            async for _ in backend.stream_agent_events(
                stub,
                [],
                "task",
                str(tmp_path),
                steering_queue=queue,
                model_wait_timeout=None,
            ):
                pass

        turn = asyncio.create_task(consume())
        while turn not in backend._ACTIVE_PROCESSES or not queue.empty():
            await asyncio.sleep(0)
        process = backend._ACTIVE_PROCESSES[turn]

        turn.cancel()
        with pytest.raises(asyncio.CancelledError):
            await turn

        assert process.returncode is not None
        assert turn not in backend._ACTIVE_PROCESSES
        assert turn not in backend._ACTIVE_STEERING
        assert turn not in backend._ACTIVE_INPUT_RESTORERS
        # Once written, this input may already have crossed a provider boundary.
        # It must not be silently requeued after caller cancellation.
        assert queue.empty()

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
        used = [e["context_used"] for e in events if e["type"] == "agent_info"]
        assert used == [None, 150, stats if type(stats) is int and stats > 0 else 150]
        assert events[-1]["type"] == "done" and events[-1]["ok"] is True

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
        assert [e["context_used"] for e in events if e["type"] == "agent_info"] == [
            None,
            78330,
            78330,
        ]
        assert all(e["context_size"] == 272000 for e in events if e["type"] == "agent_info")
        assert events[-1] == {"type": "done", "ok": True, "text": ""}

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
        assert [e["context_used"] for e in events if e["type"] == "agent_info"] == [210, 150, 150]

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
        used = [e["context_used"] for e in events if e["type"] == "agent_info"]
        assert used == (
            ([prior] if prior is not None else []) + [300, prior, 55 if stats == 55 else prior]
        )
        assert events[-1]["ok"] is True

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
        assert [e["context_used"] for e in events if e["type"] == "agent_info"] == [
            150,
            310,
            150,
            150,
        ]
        assert events[-1]["ok"] is False

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
        assert [e["context_used"] for e in events if e["type"] == "agent_info"] == [None, None]

    @pytest.mark.parametrize(
        "compaction",
        [
            {"result": None, "aborted": False},
            {"result": {"summary": "unused"}, "aborted": True},
        ],
    )
    async def test_failed_or_aborted_compaction_keeps_prior_epoch(self, tmp_path, compaction):
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
        assert [e["context_used"] for e in events if e["type"] == "agent_info"] == [
            250,
            None,
            None,
            None,
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
        assert [e["context_used"] for e in events if e["type"] == "agent_info"] == (
            [None, 250, None, None, 40, 30] if post_compaction else [None, 250, None, None, None]
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
        assert [e["context_used"] for e in events if e["type"] == "agent_info"] == [250, None, 35]

    @pytest.mark.parametrize("changed_field", ["sessionId", "sessionFile"])
    @pytest.mark.parametrize("final_state_observed", [False, True])
    async def test_stats_identity_change_fails_closed_to_unknown(
        self, tmp_path, changed_field, final_state_observed
    ):
        initial = {
            "sessionId": "first",
            "sessionFile": "/tmp/first.jsonl",
            "model": {"provider": "test", "id": "A", "contextWindow": 1000},
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
        info = [e for e in events if e["type"] == "agent_info"]
        assert [e["context_used"] for e in info] == [None, 250, None, None]
        assert all(e["session_file"] == "/tmp/first.jsonl" for e in info)
        assert all(e["context_size"] == 1000 for e in info)
        assert all(e["model"] == "test/A" for e in info)
        assert events[-1]["reason_code"] == "session_identity_uncertain"
        assert events[-1]["ok"] is False
        assert owner not in backend._ACTIVE_PROCESSES

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
        assert [e["context_used"] for e in events if e["type"] == "agent_info"] == [
            None,
            250,
            None,
        ]
        assert events[-1]["reason_code"] == "session_identity_uncertain"
        assert events[-1]["ok"] is False
        assert not any(e["type"] == "settled" for e in events)

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
        assert [e["context_used"] for e in events if e["type"] == "agent_info"] == [None, 250, None]
        assert events[-1]["reason_code"] == "session_identity_uncertain"
        assert events[-1]["ok"] is False
        assert not any(e["type"] == "settled" for e in events)

    @pytest.mark.parametrize("mutation_type", ["new_session", "switch_session", "fork", "clone"])
    async def test_outbound_session_mutation_rejected_before_write_but_a_continues(
        self, tmp_path, mutation_type
    ):
        release = tmp_path / "continue"
        stub = _stub(
            tmp_path,
            f"#!{sys.executable}\n" + """\
import json, pathlib, sys, time
release = pathlib.Path(__RELEASE__)
def emit(value):
    print(json.dumps(value), flush=True)
initial = {"sessionId": "first", "sessionFile": "/tmp/first.jsonl",
           "sessionName": "A", "model": {"provider": "test", "id": "A", "contextWindow": 1000}}
assert json.loads(sys.stdin.readline())["type"] == "get_state"
emit({"type": "response", "command": "get_state", "success": True, "data": initial})
assert json.loads(sys.stdin.readline())["type"] == "prompt"
emit({"type": "response", "command": "prompt", "id": "agent-comms-prompt", "success": True})
emit({"type": "message_start", "message": {"role": "user", "content": "task"}})
emit({"type": "tool_execution_start", "toolCallId": "a", "toolName": "bash",
      "args": {"command": "echo A"}})
emit({"type": "tool_execution_end", "toolCallId": "a", "toolName": "bash",
      "result": {"content": [{"type": "text", "text": "A"}]}, "isError": False})
emit({"type": "message_update", "assistantMessageEvent":
      {"type": "text_delta", "delta": "A-before "}})
for _ in range(300):
    if release.exists():
        break
    time.sleep(.01)
assert release.exists(), "rejection diagnostic did not wake backend"
emit({"type": "message_update", "assistantMessageEvent":
      {"type": "text_delta", "delta": "A-after"}})
emit({"type": "message_end", "message": {"role": "assistant", "stopReason": "stop",
     "usage": {"totalTokens": 250}}})
emit({"type": "agent_settled"})
assert json.loads(sys.stdin.readline())["type"] == "get_state"
assert json.loads(sys.stdin.readline())["type"] == "get_session_stats"
emit({"type": "response", "command": "get_session_stats", "success": True,
      "data": {**initial, "contextUsage": {"tokens": 250}}})
""".replace("__RELEASE__", repr(str(release))),
        )
        queue: asyncio.Queue[dict[str, str]] = asyncio.Queue()
        owner = asyncio.current_task()
        events = []
        process = None

        async def consume():
            nonlocal process
            async for event in backend.stream_agent_events(
                stub, [], "task", str(tmp_path), steering_queue=queue, model_wait_timeout=None
            ):
                events.append(event)
                if event["type"] == "chunk" and event["text"] == "A-before ":
                    process = backend._ACTIVE_PROCESSES[owner]
                    queue.put_nowait({"type": mutation_type, "id": "rejected-1"})
                if (
                    event["type"] == "error"
                    and event.get("reason_code") == "steering_command_rejected"
                ):
                    release.touch()

        async with asyncio.timeout(8):
            await consume()
        assert [e["text"] for e in events if e["type"] == "chunk"] == ["A-before ", "A-after"]
        assert [e["type"] for e in events].count("tool_end") == 1
        assert [
            (e["command"], e["id"])
            for e in events
            if e.get("reason_code") == "steering_command_rejected"
        ] == [(mutation_type, "rejected-1")]
        assert [e["type"] for e in events].count("done") == 1
        assert events[-1] == {"type": "done", "ok": True, "text": "A-before A-after"}
        assert process is not None and process.returncode == 0
        assert owner not in backend._ACTIVE_PROCESSES
        assert owner not in backend._ACTIVE_STEERING

    @pytest.mark.parametrize(
        "evidence", ["get_state", "get_session_stats", "fork_failed", "clone_cancelled"]
    )
    async def test_unsolicited_rebind_quarantines_subsequent_b_events(self, tmp_path, evidence):
        stub = _stub(
            tmp_path,
            f"#!{sys.executable}\n" + """\
import json, sys

def emit(value):
    print(json.dumps(value), flush=True)
initial = {"sessionId": "first", "sessionFile": "/tmp/first.jsonl",
           "sessionName": "A", "model": {"provider": "test", "id": "A", "contextWindow": 1000}}
changed = {"sessionId": "second", "sessionFile": "/tmp/second.jsonl",
           "sessionName": "B", "model": {"provider": "test", "id": "B", "contextWindow": 9000}}
assert json.loads(sys.stdin.readline())["type"] == "get_state"
emit({"type": "response", "command": "get_state", "success": True, "data": initial})
assert json.loads(sys.stdin.readline())["type"] == "prompt"
emit({"type": "message_update", "assistantMessageEvent":
      {"type": "text_delta", "delta": "A-before"}})
emit({"type": "message_end", "message": {"role": "assistant", "stopReason": "stop",
      "usage": {"totalTokens": 250}}})
assert json.loads(sys.stdin.readline())["message"] == "[peer] pending"
evidence = __EVIDENCE__
if evidence in {"get_state", "get_session_stats"}:
    emit({"type": "response", "command": evidence, "success": True,
          "data": {**changed, "contextUsage": {"tokens": 9999}}})
else:
    emit({"type": "response", "command": evidence.split('_')[0],
          "success": evidence.endswith("cancelled"),
          "data": {"cancelled": evidence.endswith("cancelled")}})
emit({"type": "message_start", "message": {"role": "user", "content": "[peer] pending"}})
emit({"type": "message_update", "assistantMessageEvent":
      {"type": "text_delta", "delta": "B-secret"}})
emit({"type": "message_update", "assistantMessageEvent":
      {"type": "thinking_delta", "delta": "B-secret"}})
emit({"type": "tool_execution_start", "toolCallId": "B-secret", "toolName": "bash",
      "args": {"command": "B-secret"}})
emit({"type": "tool_execution_update", "toolCallId": "B-secret", "partialResult": "B-secret"})
emit({"type": "tool_execution_end", "toolCallId": "B-secret", "toolName": "bash",
      "result": {"content": [{"type": "text", "text": "B-secret"}]}})
emit({"type": "message_end", "message": {"role": "assistant", "stopReason": "error",
      "errorMessage": "B-secret"}})
emit({"type": "auto_retry_start", "attempt": 2, "maxAttempts": 3})
emit({"type": "agent_settled"})
for line in sys.stdin:
    if json.loads(line)["type"] == "abort":
        emit({"type": "response", "command": "abort", "success": True})
        break
""".replace("__EVIDENCE__", repr(evidence)),
        )
        owner = asyncio.current_task()
        process = None
        events = []
        queue: asyncio.Queue[dict[str, str]] = asyncio.Queue()
        pending = {"type": "prompt", "message": "[peer] pending", "_input_id": "in-1"}
        async for event in backend.stream_agent_events(
            stub, [], "task", str(tmp_path), steering_queue=queue, rpc_abort_grace=0.3
        ):
            events.append(event)
            if event["type"] == "chunk":
                process = backend._ACTIVE_PROCESSES[owner]
                queue.put_nowait(pending)
        assert [e["text"] for e in events if e["type"] == "chunk"] == ["A-before"]
        assert [e["context_used"] for e in events if e["type"] == "agent_info"] == [None, 250, None]
        assert all(e.get("session_file") != "/tmp/second.jsonl" for e in events)
        assert not any(
            e["type"]
            in {"thinking", "tool_start", "tool_progress", "tool_end", "settled", "recovered"}
            for e in events
        )
        assert "B-secret" not in json.dumps(events)
        assert [e["type"] for e in events].count("done") == 1
        assert events[-1] == {
            "type": "done",
            "ok": False,
            "text": "Pi session identity changed during this turn.",
            "reason_code": "session_identity_uncertain",
        }
        assert process is not None and process.returncode is not None
        assert owner not in backend._ACTIVE_PROCESSES
        assert [e["type"] for e in events].count("input_started") == 0
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
        assert [e["context_used"] for e in events if e["type"] == "agent_info"] == [
            250,
            None,
            70,
            None,
            None,
        ]
        assert events[-1]["ok"] is False

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
        assert "[peer] ping" in steering_path.read_text()
        assert '"streamingBehavior": "steer"' in steering_path.read_text()
        assert any(
            event.get("session_file") == str(session_path)
            for event in events
            if event["type"] == "agent_info"
        )

    async def test_manual_compaction_returns_native_summary(self, tmp_path):
        args_path = tmp_path / "args"
        request_path = tmp_path / "request"
        session_path = tmp_path / "session.jsonl"
        session_path.touch()
        stub = _stub(
            tmp_path,
            f"""#!{sys.executable}
import json, pathlib, sys
pathlib.Path({str(args_path)!r}).write_text(" ".join(sys.argv[1:]))
request = sys.stdin.readline()
pathlib.Path({str(request_path)!r}).write_text(request)
print(json.dumps({{"type": "compaction_start", "reason": "manual"}}), flush=True)
print(json.dumps({{
    "id": "compact", "type": "response", "command": "compact", "success": True,
    "data": {{
        "summary": "kept decisions", "tokensBefore": 9000, "estimatedTokensAfter": 1200,
    }},
}}), flush=True)
""",
        )

        result = await backend.compact_session(
            stub, [], str(session_path), str(tmp_path), "preserve test findings"
        )

        assert result == {
            "ok": True,
            "summary": "kept decisions",
            "tokensBefore": 9000,
            "estimatedTokensAfter": 1200,
        }
        assert f"--session {session_path}" in args_path.read_text()
        assert '"customInstructions": "preserve test findings"' in request_path.read_text()

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

    async def test_accepted_prompt_stall_is_aborted_without_backend_replay(self, tmp_path):
        abort_log = tmp_path / "abort"
        launch_log = tmp_path / "launches"
        stub = _stub(
            tmp_path,
            f"#!{sys.executable}\n" + f"""\
import json, pathlib, sys, time
abort_log = pathlib.Path({str(abort_log)!r})
launch_log = pathlib.Path({str(launch_log)!r})
launch_log.write_text(launch_log.read_text() + "x" if launch_log.exists() else "x")
def emit(value):
    print(json.dumps(value), flush=True)
sys.stdin.readline()  # initial get_state
prompt = json.loads(sys.stdin.readline())
emit({{"id": prompt["id"], "type": "response", "command": "prompt", "success": True}})
emit({{"type": "message_start", "message": {{"role": "user", "content": "task"}}}})
abort = json.loads(sys.stdin.readline())
abort_log.write_text(abort["type"])
emit({{"id": abort.get("id"), "type": "response", "command": "abort", "success": True}})
time.sleep(60)
""",
        )

        events = [
            event
            async for event in backend.stream_agent_events(
                stub,
                [],
                "task",
                str(tmp_path),
                model_wait_timeout=0.03,
                rpc_abort_grace=0.1,
            )
        ]

        states = [event["state"] for event in events if event["type"] == "turn_state"]
        assert states == ["model_stalled", "aborting", "failed"]
        stalled = next(event for event in events if event.get("state") == "model_stalled")
        assert stalled["type"] == "turn_state"
        assert stalled["phase"] == "model_wait"
        assert stalled["reason_code"] == "model_no_progress"
        assert stalled["elapsed_ms"] >= 20
        # The prompt crossed an uncertain provider boundary. A watchdog may
        # abort and report diagnostics, never grant automatic replay authority.
        assert stalled["retryable"] is False and stalled["replay_safe"] is False
        assert stalled["side_effects_possible"] is True
        assert abort_log.read_text() == "abort"
        assert launch_log.read_text() == "x"  # Backend never owns prompt/session replay.
        assert [event["type"] for event in events].count("done") == 1
        assert events[-1]["type"] == "done" and events[-1]["ok"] is False
        assert "no RPC progress" in events[-1]["text"]

    async def test_watchdog_force_kills_backend_that_ignores_abort_and_term(self, tmp_path):
        stub = _stub(
            tmp_path,
            f"#!{sys.executable}\n" + """\
import json, signal, sys, time
signal.signal(signal.SIGTERM, signal.SIG_IGN)
def emit(value):
    print(json.dumps(value), flush=True)
sys.stdin.readline()
prompt = json.loads(sys.stdin.readline())
emit({"id": prompt["id"], "type": "response", "command": "prompt", "success": True})
emit({"type": "message_start", "message": {"role": "user", "content": "task"}})
time.sleep(60)
""",
        )
        started = asyncio.get_running_loop().time()

        events = [
            event
            async for event in backend.stream_agent_events(
                stub,
                [],
                "task",
                str(tmp_path),
                model_wait_timeout=0.15,
                rpc_abort_grace=0.02,
            )
        ]

        elapsed = asyncio.get_running_loop().time() - started
        states = [event["state"] for event in events if event["type"] == "turn_state"]
        assert states == ["model_stalled", "aborting", "failed"]
        assert elapsed < 1.5
        assert events[-1]["type"] == "done" and events[-1]["ok"] is False

    async def test_irrelevant_rpc_traffic_does_not_renew_model_lease(self, tmp_path):
        stub = _stub(
            tmp_path,
            f"#!{sys.executable}\n" + """\
import json, sys, time
def emit(value):
    print(json.dumps(value), flush=True)
sys.stdin.readline()
prompt = json.loads(sys.stdin.readline())
emit({"id": prompt["id"], "type": "response", "command": "prompt", "success": True})
emit({"type": "message_start", "message": {"role": "user", "content": "task"}})
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
            async for event in backend.stream_agent_events(
                stub,
                [],
                "task",
                str(tmp_path),
                model_wait_timeout=0.03,
                rpc_abort_grace=0.03,
            )
        ]

        elapsed = asyncio.get_running_loop().time() - started
        states = [event["state"] for event in events if event["type"] == "turn_state"]
        assert states == ["model_stalled", "aborting", "failed"]
        assert elapsed < 0.5

    async def test_retry_acceptance_then_silence_does_not_claim_recovery(self, tmp_path):
        stub = _stub(
            tmp_path,
            f"#!{sys.executable}\n" + """\
import json, sys, time
def emit(value):
    print(json.dumps(value), flush=True)
sys.stdin.readline()
prompt = json.loads(sys.stdin.readline())
emit({"id": prompt["id"], "type": "response", "command": "prompt", "success": True})
emit({"type": "message_start", "message": {"role": "user", "content": "task"}})
emit({"type": "auto_retry_start", "attempt": 1, "maxAttempts": 2})
emit({"type": "auto_retry_end", "success": True, "attempt": 1})
abort = json.loads(sys.stdin.readline())
emit({"id": abort.get("id"), "type": "response", "command": "abort", "success": True})
time.sleep(60)
""",
        )

        events = [
            event
            async for event in backend.stream_agent_events(
                stub,
                [],
                "task",
                str(tmp_path),
                model_wait_timeout=0.03,
                rpc_abort_grace=0.1,
            )
        ]

        states = [event["state"] for event in events if event["type"] == "turn_state"]
        assert states == ["retrying", "model_stalled", "aborting", "failed"]
        assert "recovered" not in states
        assert events[-1]["type"] == "done" and events[-1]["ok"] is False

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
        stub = _stub(tmp_path, f"#!/bin/sh\ncat <<'EOF'\n{rpc_lines}\nEOF\n")

        events = [e async for e in backend.stream_agent_events(stub, [], "task", str(tmp_path))]

        states = [event for event in events if event["type"] == "turn_state"]
        assert [event["state"] for event in states] == ["retrying", "recovered"]
        assert states[-1]["replay_safe"] is False
        assert states[-1]["side_effects_possible"] is True
        assert events[-1] == {"type": "done", "ok": True, "text": "recovered reply"}

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
        stub = _stub(tmp_path, f"#!/bin/sh\ncat <<'EOF'\n{rpc_lines}\nEOF\n")

        events = [e async for e in backend.stream_agent_events(stub, [], "task", str(tmp_path))]

        assert not [event for event in events if event["type"] == "turn_state"]
        assert events[-1] == {"type": "done", "ok": True, "text": "after compaction"}

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
        stub = _stub(tmp_path, f"#!/bin/sh\ncat <<'EOF'\n{rpc_lines}\nEOF\n")

        events = [e async for e in backend.stream_agent_events(stub, [], "task", str(tmp_path))]

        states = [event for event in events if event["type"] == "turn_state"]
        assert [event["state"] for event in states] == ["retrying", "recovered"]
        assert states[0]["reason_code"] == "overflow_compaction_retry"
        assert states[1]["reason_code"] == "overflow_retry_progress"
        assert all(event["replay_safe"] is False for event in states)
        assert all(event["side_effects_possible"] is True for event in states)

    async def test_compaction_stall_is_non_replayable(self, tmp_path):
        stub = _stub(
            tmp_path,
            f"#!{sys.executable}\n" + """\
import json, sys, time
def emit(value):
    print(json.dumps(value), flush=True)
sys.stdin.readline()
prompt = json.loads(sys.stdin.readline())
emit({"id": prompt["id"], "type": "response", "command": "prompt", "success": True})
emit({"type": "message_start", "message": {"role": "user", "content": "task"}})
emit({"type": "compaction_start", "reason": "auto"})
abort = json.loads(sys.stdin.readline())
emit({"id": abort.get("id"), "type": "response", "command": "abort", "success": True})
time.sleep(60)
""",
        )

        events = [
            event
            async for event in backend.stream_agent_events(
                stub,
                [],
                "task",
                str(tmp_path),
                model_wait_timeout=0.03,
                rpc_abort_grace=0.1,
            )
        ]

        states = [event for event in events if event["type"] == "turn_state"]
        assert [event["state"] for event in states] == [
            "model_stalled",
            "aborting",
            "failed",
        ]
        stalled = next(event for event in states if event["state"] == "model_stalled")
        assert stalled["phase"] == "compaction"
        assert stalled["reason_code"] == "compaction_no_progress"
        assert all(event["replay_safe"] is False for event in states)
        assert all(event["retryable"] is False for event in states)
        assert all(event["side_effects_possible"] is True for event in states)

    async def test_summarization_retry_stall_is_bounded_and_non_replayable(self, tmp_path):
        stub = _stub(
            tmp_path,
            f"#!{sys.executable}\n" + """\
import json, sys, time
def emit(value):
    print(json.dumps(value), flush=True)
sys.stdin.readline()
prompt = json.loads(sys.stdin.readline())
emit({"id": prompt["id"], "type": "response", "command": "prompt", "success": True})
emit({"type": "message_start", "message": {"role": "user", "content": "task"}})
emit({"type": "summarization_retry_scheduled", "attempt": 1, "maxAttempts": 2})
abort = json.loads(sys.stdin.readline())
emit({"id": abort.get("id"), "type": "response", "command": "abort", "success": True})
time.sleep(60)
""",
        )

        events = [
            event
            async for event in backend.stream_agent_events(
                stub,
                [],
                "task",
                str(tmp_path),
                model_wait_timeout=0.03,
                rpc_abort_grace=0.1,
            )
        ]

        states = [event for event in events if event["type"] == "turn_state"]
        assert [event["state"] for event in states] == [
            "retrying",
            "model_stalled",
            "aborting",
            "failed",
        ]
        stalled = next(event for event in states if event["state"] == "model_stalled")
        assert stalled["phase"] == "summarization_retry"
        assert stalled["reason_code"] == "summarization_retry_no_progress"
        assert all(event["replay_safe"] is False for event in states)
        assert all(event["side_effects_possible"] is True for event in states)

    async def test_provider_retry_failure_is_distinct_from_model_silence(self, tmp_path):
        rpc_lines = "\n".join(
            [
                '{"id":"agent-comms-prompt","type":"response","command":"prompt","success":true}',
                '{"type":"auto_retry_start","attempt":1,"maxAttempts":3,'
                '"errorMessage":"secret raw payload"}',
                '{"type":"auto_retry_end","success":false,"attempt":3,'
                '"finalError":"secret raw payload"}',
                '{"type":"message_end","message":{"role":"assistant","stopReason":"error",'
                '"errorMessage":"provider unavailable"}}',
                '{"type":"agent_settled"}',
                '{"type":"response","command":"get_session_stats","success":true,"data":'
                '{"contextUsage":{}}}',
            ]
        )
        stub = _stub(tmp_path, f"#!/bin/sh\ncat <<'EOF'\n{rpc_lines}\nEOF\n")

        events = [
            event
            async for event in backend.stream_agent_events(
                stub, [], "task", str(tmp_path), model_wait_timeout=0.03
            )
        ]

        recovery = [event for event in events if event["type"] == "turn_state"]
        assert [event["state"] for event in recovery] == ["retrying", "failed"]
        assert recovery[0]["attempt"] == {"current": 1, "max": 3}
        assert recovery[-1]["reason_code"] == "provider_retry_exhausted"
        assert all("secret" not in str(event) for event in recovery)
        assert not any(event.get("state") == "model_stalled" for event in recovery)
        assert events[-1]["ok"] is False
        assert events[-1]["text"] == "provider unavailable"

    @pytest.mark.parametrize("started", [False, True])
    async def test_watchdog_never_requeues_forwarded_input_after_uncertainty(
        self, tmp_path, started
    ):
        steering_log = tmp_path / "steering"
        stub = _stub(
            tmp_path,
            f"#!{sys.executable}\n" + f"""\
import json, pathlib, sys, time
def emit(value):
    print(json.dumps(value), flush=True)
sys.stdin.readline()
prompt = json.loads(sys.stdin.readline())
emit({{"id": prompt["id"], "type": "response", "command": "prompt", "success": True}})
emit({{"type": "message_start", "message": {{"role": "user", "content": "task"}}}})
steering = json.loads(sys.stdin.readline())
pathlib.Path({str(steering_log)!r}).write_text(json.dumps(steering, sort_keys=True))
emit({{"id": steering["id"], "type": "response", "command": "prompt", "success": True}})
if {started!r}:
    emit({{"type": "message_start", "message": {{"role": "user", "content": "follow"}}}})
time.sleep(0.06)
abort = json.loads(sys.stdin.readline())
emit({{"id": abort.get("id"), "type": "response", "command": "abort", "success": True}})
time.sleep(60)
""",
        )
        original = {
            "type": "prompt",
            "message": "follow",
            "streamingBehavior": "followUp",
            "_input_id": "queued-1",
            "images": [{"type": "image", "data": "abc", "mimeType": "image/png"}],
        }
        queue: asyncio.Queue[str | dict] = asyncio.Queue()
        queue.put_nowait(original)

        events = [
            event
            async for event in backend.stream_agent_events(
                stub,
                [],
                "task",
                str(tmp_path),
                steering_queue=queue,
                model_wait_timeout=0.03,
                rpc_abort_grace=0.2,
            )
        ]

        sent = steering_log.read_text()
        assert '"_input_id"' not in sent and '"images"' in sent
        started_events = [event for event in events if event["type"] == "input_started"]
        assert started_events == ([{"type": "input_started", "id": "queued-1"}] if started else [])
        assert events[-1]["ok"] is False
        # Start evidence informs disposition, but neither case authorizes an
        # automatic replay of the forwarded provider opportunity.
        assert queue.empty()

    async def test_watchdog_never_times_out_an_active_tool(self, tmp_path):
        stub = _stub(
            tmp_path,
            f"#!{sys.executable}\n" + """\
import json, sys, time
def emit(value):
    print(json.dumps(value), flush=True)
sys.stdin.readline()
prompt = json.loads(sys.stdin.readline())
emit({"id": prompt["id"], "type": "response", "command": "prompt", "success": True})
emit({"type": "message_start", "message": {"role": "user", "content": "task"}})
emit({"type": "tool_execution_start", "toolCallId": "slow", "toolName": "bash", "args": {}})
time.sleep(0.12)
emit({"type": "tool_execution_end", "toolCallId": "slow", "toolName": "bash",
      "result": {"content": [{"type": "text", "text": "finished"}]}, "isError": False})
emit({"type": "message_update", "assistantMessageEvent": {
    "type": "text_delta", "delta": "done after tool"}})
emit({"type": "message_end", "message": {"role": "assistant", "stopReason": "stop"}})
emit({"type": "agent_settled"})
for line in sys.stdin:
    command = json.loads(line)["type"]
    if command == "get_session_stats":
        emit({"type": "response", "command": command, "success": True,
              "data": {"contextUsage": {}}})
        break
""",
        )

        events = [
            event
            async for event in backend.stream_agent_events(
                stub, [], "task", str(tmp_path), model_wait_timeout=0.03
            )
        ]

        assert not [event for event in events if event["type"] == "turn_state"]
        assert events[-1] == {"type": "done", "ok": True, "text": "done after tool"}

    async def test_stall_after_tool_is_failed_without_retry(self, tmp_path):
        launches = tmp_path / "launches"
        stub = _stub(
            tmp_path,
            f"#!{sys.executable}\n" + f"""\
import json, pathlib, sys, time
launches = pathlib.Path({str(launches)!r})
launches.write_text(launches.read_text() + "x" if launches.exists() else "x")
def emit(value):
    print(json.dumps(value), flush=True)
sys.stdin.readline()
prompt = json.loads(sys.stdin.readline())
emit({{"id": prompt["id"], "type": "response", "command": "prompt", "success": True}})
emit({{"type": "message_start", "message": {{"role": "user", "content": "task"}}}})
emit({{"type": "tool_execution_start", "toolCallId": "write", "toolName": "write", "args": {{}}}})
emit({{"type": "tool_execution_end", "toolCallId": "write", "toolName": "write",
      "result": {{"content": []}}, "isError": False}})
for line in sys.stdin:
    if json.loads(line)["type"] == "abort":
        emit({{"type": "response", "command": "abort", "success": True}})
        time.sleep(60)
""",
        )

        events = [
            event
            async for event in backend.stream_agent_events(
                stub,
                [],
                "task",
                str(tmp_path),
                model_wait_timeout=0.03,
                rpc_abort_grace=0.1,
            )
        ]

        states = [event["state"] for event in events if event["type"] == "turn_state"]
        assert states == ["model_stalled", "aborting", "failed"]
        failed = next(event for event in events if event.get("state") == "failed")
        assert failed["phase"] == "shutdown"
        assert failed["reason_code"] == "model_no_progress"
        assert failed["retryable"] is False and failed["replay_safe"] is False
        assert failed["side_effects_possible"] is True
        assert launches.read_text() == "x"
        assert events[-1]["type"] == "done" and events[-1]["ok"] is False
        assert "no RPC progress" in events[-1]["text"]

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
        stub = _stub(tmp_path, f"#!/bin/sh\ncat <<'EOF'\n{rpc_lines}\nEOF\n")
        events = [e async for e in backend.stream_agent_events(stub, [], "t", str(tmp_path))]
        assert events[-1]["ok"] is True
        tool_end = events[1]
        assert tool_end["ok"] is False


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

    def test_model_arguments_are_read_and_replaced(self):
        args = ["--print", "--provider", "openrouter", "--model", "old/model"]
        assert backend.configured_model(args) == "openrouter/old/model"
        assert backend.args_for_model(args, "anthropic/claude-sonnet") == [
            "--print",
            "--provider",
            "anthropic",
            "--model",
            "claude-sonnet",
        ]

    async def test_model_catalog_comes_from_pi_rpc(self, tmp_path):
        stub = _stub(
            tmp_path,
            """#!/bin/sh
IFS= read -r request
cat <<'EOF'
{"id":"models","type":"response","command":"get_available_models","success":true,"data":{"models":[{"provider":"openrouter","id":"z-ai/glm"},{"provider":"anthropic","id":"claude"}]}}
EOF
""",
        )

        models = await backend.discover_models(stub, [], "openrouter/current")

        assert [model.id for model in models] == [
            "openrouter/current",
            "openrouter/z-ai/glm",
            "anthropic/claude",
        ]

    def test_tool_kind_mapping(self):
        assert backend.tool_kind("bash") == "execute"
        assert backend.tool_kind("read") == "read"
        assert backend.tool_kind("weird") == "other"


# Native per-input proof tests retained from the PR#1 parent.
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
        self, tmp_path, preflight
    ):
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
        assert events[-1]["reason_code"] == "pi_input_id_unavailable"
        assert "preflight" in events[-1]["text"]

    @pytest.mark.parametrize("phase", ["preflight", "no_user_start"])
    async def test_broken_child_stdin_close_still_reaps_and_reports_typed_failure(
        self, tmp_path, monkeypatch, phase
    ):
        pid_file = tmp_path / "child.pid"
        stub = _stub(
            tmp_path,
            f"#!{sys.executable}\n" + f"""
import json, os, signal, sys, time
state = json.loads(sys.stdin.readline())
assert state["type"] == "get_state"
if {phase!r} == "preflight":
    data = "invalid-state"
else:
    data = {{"nativeInputProofCapability": "pi-native-input-v1-live-only"}}
print(json.dumps({{"type": "response", "command": "get_state",
                  "id": state["id"], "success": True, "data": data}}), flush=True)
if {phase!r} == "no_user_start":
    prompt = json.loads(sys.stdin.readline())
    print(json.dumps({{"type": "response", "command": "prompt",
                      "id": prompt["id"], "success": True}}), flush=True)
open({str(pid_file)!r}, "w").write(str(os.getpid()))
signal.signal(signal.SIGTERM, lambda *_: None)
while True: time.sleep(0.1)
""",
        )
        create = backend.asyncio.create_subprocess_exec
        close_calls = []

        async def spawn(*args, **kwargs):
            proc = await create(*args, **kwargs)
            assert proc.stdin is not None
            original_close = proc.stdin.close

            def fail_after_close():
                original_close()
                close_calls.append(True)
                raise BrokenPipeError("child closed its stdin read end")

            monkeypatch.setattr(proc.stdin, "close", fail_after_close)
            return proc

        monkeypatch.setattr(backend.asyncio, "create_subprocess_exec", spawn)
        monkeypatch.setattr(backend, "PROMPT_START_TIMEOUT_SECONDS", 0.1)
        async with asyncio.timeout(4):
            events = [
                event
                async for event in backend.stream_agent_events(stub, [], "work", str(tmp_path))
            ]
        assert events[-1]["type"] == "done" and events[-1]["ok"] is False
        assert events[-1]["reason_code"] == (
            "pi_input_id_unavailable" if phase == "preflight" else "current_prompt_input_missing"
        )
        assert pid_file.exists() and close_calls
        with pytest.raises(ProcessLookupError):
            os.kill(int(pid_file.read_text()), 0)
        assert not backend._ACTIVE_PROCESSES
        assert not backend._ACTIVE_STDERR_TASKS

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


# PR#1 parser assertions retained alongside V3 projection/watchdog coverage.
class TestPrRpcParsing:
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
    async def test_no_matching_preflight_before_user_start_fails_closed(self, tmp_path, records):
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
        assert events[-1]["reason_code"] == "current_prompt_input_missing"

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
