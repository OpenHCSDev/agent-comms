"""Continuous saved-history recovery through the verified native CLI and HTTP.

Only local provider replies are controlled. The native run, refusal backoff,
Core event parsing and original session journal are real. No silence timeout.
"""

import asyncio
import json
from collections import deque
from pathlib import Path

import pytest

from agent_comms import agent_events as ae, backend
from agent_comms.pi_native_backend import PersistentPiSession
from compaction_loopback import LoopbackProvider

CODEX_RETRYABLE = (
    "Codex error: An error occurred while processing your request. You can retry your request, "
    "or contact us through our help center at help.openai.com if the error persists."
)


@pytest.mark.asyncio
async def test_actual_native_refusal_after_tool_preserves_saved_history(tmp_path, monkeypatch):
    marker = tmp_path / "read-once.txt"
    marker.write_text("tool completed before provider refusal")
    tool_reply = LoopbackProvider(status=200)
    tool_reply.tool_call = ("read", {"path": str(marker)})
    replies = deque([
        LoopbackProvider(status=200, text="saved history before recovery"),
        tool_reply,
        *(LoopbackProvider(status=503, error_message=CODEX_RETRYABLE) for _ in range(3)),
        LoopbackProvider(status=200, text="answered after three refusals"),
    ])
    received = []
    tasks = set()

    async def serve(reader, writer):
        task = asyncio.current_task()
        tasks.add(task)
        try:
            reply = replies.popleft()
            received.append(reply)
            await reply.handle(reader, writer)
        finally:
            tasks.discard(task)
            writer.close()
            await writer.wait_closed()

    server = await asyncio.start_server(serve, "127.0.0.1", 0)
    config = tmp_path / "config"
    config.mkdir(mode=0o700)
    (config / "models.json").write_text(json.dumps({"providers": {"retry-offline": {
        "baseUrl": f"http://127.0.0.1:{server.sockets[0].getsockname()[1]}/v1",
        "api": "openai-completions", "models": [{"id": "fixture", "name": "Local",
            "contextWindow": 32768, "maxTokens": 2048}],
    }}}))
    (config / "auth.json").write_text(json.dumps({"retry-offline": {
        "type": "api_key", "key": "offline-only-fixture",
    }}))
    (config / "settings.json").write_text(json.dumps({
        "compaction": {"enabled": False}, "retry": {"enabled": False, "maxRetries": 0,
            "provider": {"maxRetries": 0}},
    }))
    monkeypatch.setenv("PI_CODING_AGENT_DIR", str(config))
    monkeypatch.setenv("PI_PROVIDER", "retry-offline")
    monkeypatch.setenv("PI_MODEL", "fixture")
    monkeypatch.setenv("PI_REASONING_LEVEL", "off")
    # An explicit isolated root suppresses discovery of Tristan's installed route.
    monkeypatch.setenv("AGENT_COMMS_ROOT", str(tmp_path / "wire"))
    launcher = str(Path(__file__).parents[1] / "stack/bin/pi-native")
    arguments = ["--provider", "retry-offline", "--model", "fixture", "--thinking", "off",
                 "--no-extensions", "--no-skills", "--no-context-files"]
    session = PersistentPiSession()
    saved = None

    async def turn(text):
        return [event async for event in backend.stream_agent_events(
            launcher, arguments, text, str(tmp_path), session_file=saved,
            persistent_session=session, require_input_id=True, model_wait_timeout=240,
        )]

    try:
        async with asyncio.timeout(240):
            seed = await turn("Seed new isolated history")
            assert isinstance(seed[-1], ae.Done) and seed[-1].ok, seed
            saved = next(event.session_file for event in seed
                         if isinstance(event, ae.AgentInfo) and event.session_file)
            original = Path(saved).read_bytes()
            recovered = await turn("Read the marker, then survive three empty refusals")
            assert isinstance(recovered[-1], ae.Done) and recovered[-1].ok, recovered
            assert recovered[-1].text == "answered after three refusals"
            assert not any(isinstance(event, ae.Error) for event in recovered)
            assert len([event for event in recovered if isinstance(event, ae.ToolStart)]) == 1
            assert [event.attempt.attempt for event in recovered
                    if isinstance(event, ae.TurnState) and event.state == "retrying"] == [1, 2, 3]
            assert Path(saved).read_bytes().startswith(original)
            assert not replies and len(received) == 6
            assert all(reply.posts == 1 for reply in received)
            journal = [json.loads(line) for line in Path(saved).read_text().splitlines()]
            users = [row["message"] for row in journal
                     if row.get("type") == "message" and row["message"]["role"] == "user"]
            assert len(users) == 2  # Each new input committed once, never replayed.
            assert len({message["inputId"] for message in users}) == 2
            failures = [row["message"] for row in journal
                        if row.get("type") == "message"
                        and row["message"].get("stopReason") in {"error", "aborted"}]
            assert len(failures) == 3  # All original refusals remain in saved history.
    finally:
        await session.close()
        server.close()
        await server.wait_closed()
        remaining = tuple(tasks)
        for task in remaining:
            task.cancel()
        await asyncio.gather(*remaining, return_exceptions=True)
