"""Streaming backend: rpc parsing, text fallback, failure handling."""

import asyncio
import sys
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


class TestRpcParsing:
    async def test_full_rpc_stream(self, tmp_path):
        """Canned pi rpc stream -> chunk/tool_start/tool_end/done events."""
        rpc_lines = "\n".join(
            [
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
        assert '"message": "[peer] ping"' in steering_path.read_text()
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

    async def test_failed_tool_does_not_fail_recovered_turn(self, tmp_path):
        rpc_lines = "\n".join(
            [
                '{"type":"tool_execution_start","toolCallId":"t1","toolName":"bash","args":{}}',
                '{"type":"tool_execution_end","toolCallId":"t1","toolName":"bash","result":{"content":[{"type":"text","text":"boom"}]},"isError":true}',
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

    def test_tool_kind_mapping(self):
        assert backend.tool_kind("bash") == "execute"
        assert backend.tool_kind("read") == "read"
        assert backend.tool_kind("weird") == "other"
