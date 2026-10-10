"""Pi's own compaction engine, seen through Core's turn path: pinned Pi + localhost provider."""

from __future__ import annotations

from agent_comms.queued_input import InitialInput
import asyncio
import json
import os
import sys
import threading
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import uuid4

import pytest

from agent_comms import agent_events as ae
from agent_comms import backend
from agent_comms.acp import CommsAgent
from agent_comms.acp_extension import CompactionChangedUpdate, decode_updates
from agent_comms.activity import ActivityState
from agent_comms.comms import wire
from agent_comms.input_disposition import InputDispositions
from agent_comms.pi_native_backend import PersistentPiSession
from agent_comms.pi_vocabulary import OverflowCompactionReason, ThresholdCompactionReason

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="native Pi uses POSIX fsync")

SUMMARY = "PI_ENGINE_SUMMARY"
OVERFLOW_ERROR = (
    "This endpoint's maximum context length is 128000 tokens. However, you requested "
    "about 300000 tokens (299000 of text input, 1000 in the output)."
)


def _saved_history(path: Path, cwd: Path, *, used: int) -> None:
    """A real Pi branch; ``used`` is the last measured context of every assistant."""
    timestamp = "2026-09-24T00:00:00.000Z"
    rows: list[dict] = [{
        "type": "session", "version": 3, "id": str(uuid4()), "timestamp": timestamp,
        "cwd": str(cwd), "parentSession": None,
    }]
    parent = None
    for index in range(100):
        user_id, assistant_id = f"{2 * index + 1:08x}", f"{2 * index + 2:08x}"
        text = ("LEGACY_DISCARDED_HISTORY " if index == 0 else "") + "x" * 6000
        rows.append({
            "type": "message", "id": user_id, "parentId": parent, "timestamp": timestamp,
            "message": {"role": "user", "content": [{"type": "text", "text": text}],
                        "timestamp": 1790290000000 + 2 * index},
        })
        rows.append({
            "type": "message", "id": assistant_id, "parentId": user_id, "timestamp": timestamp,
            "message": {
                "role": "assistant", "content": [{"type": "text", "text": "ack"}],
                "api": "openai-completions", "provider": "openrouter",
                "model": "z-ai/glm-5.3-flash", "stopReason": "stop", "rawStopReason": "stop",
                "timestamp": 1790290000001 + 2 * index,
                "usage": {"input": used, "output": 1, "cacheRead": 0, "cacheWrite": 0,
                          "reasoning": 0, "totalTokens": used + 1,
                          "cost": {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0,
                                   "total": 0}},
            },
        })
        parent = assistant_id
    path.write_text("".join(json.dumps(row, separators=(",", ":")) + "\n" for row in rows))
    path.chmod(0o600)


def _is_summary(request: dict) -> bool:
    return "context summarization assistant" in json.dumps(request["messages"][:1])


@pytest.mark.parametrize("case", [
    "threshold", "acp_threshold", "overflow", "compacted_resume", "summary_failure",
    "post_compaction_tool_rounds",
])
async def test_pi_engine_compacts_inside_a_core_turn(case: str, monkeypatch) -> None:
    native_bin = os.environ.get("AC_NATIVE_STACK_BIN")
    if not native_bin:
        pytest.skip("Set AC_NATIVE_STACK_BIN to the prepared pinned Pi launcher")
    with TemporaryDirectory(prefix="ac-native-compaction-") as raw:
        root = Path(raw)
        project = root / "project"
        (project / ".pi").mkdir(parents=True, mode=0o700)
        (project / ".pi" / "settings.json").write_text(json.dumps({
            "retry": {"enabled": False, "maxRetries": 0, "provider": {"maxRetries": 0}},
            "compaction": {"enabled": True},
        }))
        session = root / "saved.jsonl"
        # Above the threshold (128000 - 16384) and below the window, except for overflow.
        _saved_history(session, project, used=1000 if case == "overflow" else 120000)
        if case == "compacted_resume":
            rows = [json.loads(line) for line in session.read_text().splitlines()]
            boundary = datetime.fromtimestamp(1790290000.200, UTC).isoformat()
            with session.open("a") as stream:
                stream.write(json.dumps({
                    "type": "compaction", "id": "compact1", "parentId": rows[-1]["id"],
                    "timestamp": boundary, "summary": "Earlier work completed.",
                    "firstKeptEntryId": rows[-2]["id"], "tokensBefore": 200001,
                }) + "\n")
        if case == "post_compaction_tool_rounds":
            for index in range(3):
                (project / f"read-{index}.txt").write_text(f"TOOL_ROUND_{index}\n")
        agent = root / "agent"
        agent.mkdir(mode=0o700)
        summaries: list[dict] = []
        prompts: list[dict] = []
        summary_entered = threading.Event()
        release_summary = threading.Event()

        class Handler(BaseHTTPRequestHandler):
            def reply(self, status: int, body: bytes, kind: str) -> None:
                self.send_response(status)
                self.send_header("Content-Type", kind)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_POST(self):
                request = json.loads(self.rfile.read(int(self.headers.get("Content-Length", "0"))))
                summary = _is_summary(request)
                (summaries if summary else prompts).append(request)
                if summary and case == "acp_threshold":
                    summary_entered.set()
                    assert release_summary.wait(timeout=15), "Test did not release compaction"
                if summary and case == "summary_failure":
                    self.reply(429, b'{"error":{"message":"429 rate limit","type":"rate_limit_error"}}',
                               "application/json")
                    return
                if not summary and case == "overflow" and len(prompts) == 1:
                    body = json.dumps({"error": {"message": OVERFLOW_ERROR, "code": 400}}).encode()
                    self.reply(400, body, "application/json")
                    return
                delta: dict = {"role": "assistant", "content": SUMMARY if summary else "OK_REPLY"}
                finish = "stop"
                if not summary and case == "post_compaction_tool_rounds" and len(prompts) <= 3:
                    delta = {"role": "assistant", "tool_calls": [{
                        "index": 0, "id": f"post_compact_{len(prompts)}", "type": "function",
                        "function": {"name": "read", "arguments": json.dumps(
                            {"path": str(project / f"read-{len(prompts) - 1}.txt")})},
                    }]}
                    finish = "tool_calls"
                chunk = {"id": f"fixture-{len(summaries) + len(prompts)}",
                         "object": "chat.completion.chunk", "created": 12345,
                         "model": "z-ai/glm-5.3-flash",
                         "choices": [{"index": 0, "delta": delta, "finish_reason": None}]}
                terminal = {**chunk, "choices": [{"index": 0, "delta": {}, "finish_reason": finish}],
                            "usage": {"prompt_tokens": 100, "completion_tokens": 3,
                                      "total_tokens": 103}}
                body = ("".join(f"data: {json.dumps(row)}\n\n" for row in (chunk, terminal))
                        + "data: [DONE]\n\n").encode()
                self.reply(200, body, "text/event-stream")

            def log_message(self, *_args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        events: list[ae.AgentEvent] = []
        try:
            (agent / "models.json").write_text(json.dumps({"providers": {"openrouter": {
                "baseUrl": f"http://127.0.0.1:{server.server_port}/v1",
                "modelOverrides": {"z-ai/glm-5.3-flash": {"contextWindow": 128000,
                                                          "maxTokens": 4096}},
            }}}))
            (agent / "models.json").chmod(0o600)
            preload = root / "local-only.cjs"
            preload.write_text(
                "const original=globalThis.fetch;"
                "globalThis.fetch=(url,...rest)=>{"
                "const link=url instanceof Request?url.url:String(url);"
                f"if(!link.startsWith('http://127.0.0.1:{server.server_port}/')) "
                "throw new Error('BLOCKED_NONLOCAL_NETWORK');"
                "return original(url,...rest);};"
            )
            native_args = [
                "--offline", "--no-extensions", "--no-skills", "--no-prompt-templates",
                "--no-context-files", "--no-tools", "--provider", "openrouter",
                "--model", "z-ai/glm-5.3-flash", "--thinking", "high",
            ]
            if case == "post_compaction_tool_rounds":
                native_args.remove("--no-tools")
                native_args.extend(["--tools", "read"])
            child_env = {
                "PI_CODING_AGENT_DIR": str(agent),
                "OPENROUTER_API_KEY": "offline-fixture-no-real-key",
                "NODE_OPTIONS": f"--require={preload}",
                "PI_OFFLINE": "1",
                "PI_TASK": "TASK_BRIEF_SENTINEL",
            }
            if case == "acp_threshold":
                await _acp_threshold(root, project, session, native_bin, native_args, child_env,
                                     summary_entered, release_summary, monkeypatch)
                assert len(summaries) == 1 and len(prompts) == 1
                assert "TASK_BRIEF_SENTINEL" in json.dumps(summaries[0]["messages"])
                return
            pi_session = PersistentPiSession()
            try:
                async with asyncio.timeout(30):
                    async for event in backend.stream_agent_events(
                        native_bin, native_args, "Reply OK.", str(project),
                        env_extra=child_env, session_file=str(session),
                        require_input_id=True, native_start=lambda *_: True,
                        persistent_session=pi_session,
                    ):
                        events.append(event)
            finally:
                await pi_session.close()
        finally:
            release_summary.set()
            server.shutdown()
            server.server_close()
            worker.join(timeout=2)

        saved = [json.loads(line) for line in session.read_text().splitlines()]
        compactions = [row for row in saved if row.get("type") == "compaction"]
        ends = [event for event in events if isinstance(event, ae.CompactionEnd)]
        done = events[-1]
        assert isinstance(done, ae.Done)
        if case == "compacted_resume":
            # The saved compaction already bounds the context; its stale usage triggers nothing.
            assert done.ok is True and not ends and not summaries and len(prompts) == 1
            assert len(compactions) == 1
            return
        if case == "summary_failure":
            # Pi's engine decides: its failed pre-prompt compaction is reported, the
            # prompt is still sent once, and nothing is written as a compaction.
            assert summaries and len(prompts) == 1 and compactions == []
            assert len(ends) == 1 and ends[0].aborted
            assert done.ok is True
            return
        assert done.ok is True, done
        assert len(ends) == 1 and not ends[0].aborted
        assert len(compactions) == 1 and SUMMARY in compactions[0]["summary"]
        assert len(summaries) == 1
        assert "TASK_BRIEF_SENTINEL" in json.dumps(summaries[0]["messages"])
        assert "LEGACY_DISCARDED_HISTORY" in json.dumps(summaries[0]["messages"])
        final = json.dumps(prompts[-1]["messages"])
        assert SUMMARY in final and "LEGACY_DISCARDED_HISTORY" not in final
        if case == "threshold":
            # Pi compacts before the prompt; the input is sent once, after the summary.
            assert ends[0].reason is ThresholdCompactionReason
            assert len(prompts) == 1
            kinds = [type(event) for event in events]
            assert kinds.index(ae.CompactionEnd) < kinds.index(ae.InputStarted)
            return
        if case == "overflow":
            # The refused request is resent once after Pi's overflow compaction; the turn succeeds.
            assert ends[0].reason is OverflowCompactionReason and ends[0].will_retry
            assert len(prompts) == 2
            assert "LEGACY_DISCARDED_HISTORY" in json.dumps(prompts[0]["messages"])
            return
        assert case == "post_compaction_tool_rounds"
        assert len(prompts) == 4
        for index, request in enumerate(prompts):
            serialized = json.dumps(request["messages"])
            assert "LEGACY_DISCARDED_HISTORY" not in serialized, f"history resurrected in {index}"
            tool_calls = [call["id"] for message in request["messages"]
                          for call in message.get("tool_calls", [])]
            tool_results = [message["tool_call_id"] for message in request["messages"]
                            if message["role"] == "tool"]
            assert tool_calls == tool_results == [f"post_compact_{n}" for n in range(1, index + 1)]
            for n in range(index):
                assert f"TOOL_ROUND_{n}" in serialized


async def _acp_threshold(root, project, session, native_bin, native_args, child_env,
                         summary_entered, release_summary, monkeypatch) -> None:
    """The owner's turn shows 'Compacting context' while Pi summarizes, then replies."""
    for key, value in child_env.items():
        monkeypatch.setenv(key, value)
    comms = wire(root / "wire")
    root_id = comms.messaging.initialize_private_initial_protocol()
    package_path = os.environ.get("PI_COMPACTION_TEST_PACKAGE")
    assert package_path, "Set PI_COMPACTION_TEST_PACKAGE to the verified native package"
    package = Path(package_path)
    monkeypatch.setenv("AGENT_COMMS_ROOT", str(comms.root))
    monkeypatch.setenv("AGENT_COMMS_PRIVATE_NK_WIRE_ROOT_ID", root_id)
    monkeypatch.setenv("AGENT_COMMS_PRIVATE_NK_NATIVE_PACKAGE", str(package))
    owner = CommsAgent(
        comms, agent_bin=native_bin, agent_args=native_args, runtime_enabled=True,
        auto_wake=False, private_nk_native_package=package, private_nk_wire_root_id=root_id,
    )
    updates = []
    resumed_activity = []

    class Client:
        async def session_update(self, session_id, update):
            updates.append(update)
            if any(isinstance(field, CompactionChangedUpdate)
                   and isinstance(field.event, ae.CompactionEnd)
                   for field in decode_updates(update.field_meta or {})):
                resumed_activity.append(comms.agents.activity_of("project"))

    owner.on_connect(Client())
    await owner.new_session(str(project))
    owner.inputs.drain_tasks["project"].cancel()
    await asyncio.gather(owner.inputs.drain_tasks["project"], return_exceptions=True)
    comms.threads.attach_session(comms.registry.require("project"), str(session))
    turn = asyncio.create_task(InitialInput.run(owner.inputs, "project", "project", "Reply OK."))
    try:
        entered = await asyncio.to_thread(summary_entered.wait, 10)
        assert entered
        # Pi announced compaction_start before its summary request; Core publishes it.
        async with asyncio.timeout(5):
            while (activity := comms.agents.activity_of("project")).state is not ActivityState.WORKING:
                await asyncio.sleep(0.02)
        assert activity.detail == "Compacting context"
        release_summary.set()
        await asyncio.wait_for(turn, timeout=30)
        assert resumed_activity and "Compacting" not in resumed_activity[-1].detail
        assert comms.agents.activity_of("project").state is ActivityState.IDLE
    finally:
        release_summary.set()
        if not turn.done():
            turn.cancel()
        await asyncio.gather(turn, return_exceptions=True)
        await owner.shutdown()
    rows = InputDispositions(comms.root / InputDispositions.filename).read().rows
    assert len(rows) == 1 and not next(iter(rows.values())).unresolved
    compaction_events = [field.event for update in updates
                         for field in decode_updates(update.field_meta or {})
                         if isinstance(field, CompactionChangedUpdate)]
    assert [type(event) for event in compaction_events] == [ae.CompactionStart, ae.CompactionEnd]
    assert compaction_events[-1].reason is ThresholdCompactionReason
    texts = [getattr(getattr(update, "content", None), "text", "") for update in updates
             if getattr(update, "session_update", None) == "agent_message_chunk"]
    assert any("OK_REPLY" in text for text in texts)
    assert not any("[agent error]" in text for text in texts)
    saved = [json.loads(line) for line in session.read_text().splitlines()]
    assert [row["summary"] for row in saved if row.get("type") == "compaction"] == [SUMMARY]
