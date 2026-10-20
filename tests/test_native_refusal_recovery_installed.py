"""Saved-history recovery through an owned native turn and localhost HTTP."""

import asyncio
import json
from collections import deque
from pathlib import Path

import pytest

import agent_comms.agent_events as ae
from compaction_loopback import LoopbackProvider

CODEX_RETRYABLE = (
    "Codex error: An error occurred while processing your request. You can retry your request, "
    "or contact us through our help center at help.openai.com if the error persists."
)


@pytest.mark.asyncio
async def test_actual_native_refusal_after_tool_preserves_saved_history(native_backend, monkeypatch):
    owner = native_backend
    marker = owner.project / "read-once.txt"
    marker.write_text("tool completed before provider refusal")
    tool_reply = LoopbackProvider(status=200)
    tool_reply.tool_call = ("read", {"path": str(marker)})
    replies = deque([
        LoopbackProvider(status=200, text="saved history before recovery"),
        tool_reply,
        *(LoopbackProvider(status=503, error_message=CODEX_RETRYABLE) for _ in range(3)),
        LoopbackProvider(status=200, text="answered after three refusals"),
    ])
    received, tasks = [], set()

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
    model_file = owner.config / "models.json"
    config = json.loads(model_file.read_text())
    config["providers"]["response-local"]["baseUrl"] = f"http://127.0.0.1:{server.sockets[0].getsockname()[1]}/v1"
    model_file.write_text(json.dumps(config))
    try:
        async with asyncio.timeout(240), owner.open_owner(native_options=("--tools", "read")) as (agent, session):
            native = agent.turns.persistent_backends[session]
            original_stream = native.run_turn
            observed = []

            async def capture(*args, **kwargs):
                async for event in original_stream(*args, **kwargs):
                    observed.append(event)
                    yield event

            monkeypatch.setattr(native, "run_turn", capture)

            async def turn(text):
                observed.clear()
                await agent.prompt(session, [{"type": "text", "text": text}])
                return observed.copy()

            seed = await turn("Seed new isolated history")
            assert isinstance(seed[-1], ae.Done) and seed[-1].ok, seed[-1].text
            saved = next(event.session_file for event in seed
                         if isinstance(event, ae.AgentInfo) and event.session_file)
            original = Path(saved).read_bytes()
            recovered = await turn("Read the marker, then survive three empty refusals")
            assert isinstance(recovered[-1], ae.Done) and recovered[-1].ok, recovered[-1].text
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
            assert len(users) == 2
            assert len({message["inputId"] for message in users}) == 2
            failures = [row["message"] for row in journal
                        if row.get("type") == "message"
                        and row["message"].get("stopReason") in {"error", "aborted"}]
            assert len(failures) == 3
    finally:
        server.close()
        await server.wait_closed()
        remaining = tuple(tasks)
        for task in remaining:
            task.cancel()
        await asyncio.gather(*remaining, return_exceptions=True)
