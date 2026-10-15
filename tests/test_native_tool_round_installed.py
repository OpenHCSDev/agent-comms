"""One fresh installed native journey covers delayed rounds and error settlement."""

import asyncio
import json
from pathlib import Path

import pytest

from agent_comms.channel_coding_tools import CodingToolOwner, CodingToolSocket
from agent_comms.pi_payloads import ToolCallContent
from agent_comms.tracked_turn import TrackedTurnSession
from delivery_owner_fixture import reply_turn_finished
from test_backend_native_lifecycle import native_backend


async def test_actual_native_delayed_round_then_tool_effect_and_error(native_backend, monkeypatch):
    native = native_backend
    await native.author_history()
    original = native.session.read_bytes()
    (native.project / "independent-read.txt").write_text("Fresh fixture, not a retained input\n")
    effect = native.project / "independent-effect.txt"
    release = asyncio.Event()
    requests, admissions, sockets = [], [], []
    provider = native.provider

    def response_chunks():
        number = provider.posts
        assert number in (1, 2), "No provider retry or input replay is expected"
        name, arguments = (
            ("read", {"path": "independent-read.txt"}) if number == 1 else
            ("bash", {"command": "printf 'fresh-effect\\n' >> independent-effect.txt"})
        )
        yield {"tool_calls": [{
            "index": 0, "id": f"fresh-round-{number}", "type": "function",
            "function": {"name": name, "arguments": json.dumps(arguments)},
        }]}, None
        yield {}, "tool_calls"
        if number == 2:
            provider.status = 400
            provider.error_message = "controlled terminal after the distinct admitted effect"

    message_end = TrackedTurnSession.message_end

    async def delayed_declaration(turn, event):
        if any(isinstance(item, ToolCallContent) and item.id == "fresh-round-1"
               for item in event.message.content):
            # The actual first socket request reaches its original ten-second
            # start deadline. The second request must exist before this native
            # declaration is consumed, reproducing the retained ordering.
            await asyncio.wait_for(release.wait(), 20)
        await message_end(turn, event)

    handle_request = CodingToolSocket.handle_request

    async def observed_request(socket, raw):
        call_id = json.loads(raw)["call_id"]
        requests.append(call_id)
        if socket not in sockets:
            sockets.append(socket)
        if call_id == "fresh-round-2":
            # handle_request registers its unresolved call before yielding.
            asyncio.get_running_loop().call_soon(release.set)
        return await handle_request(socket, raw)

    admit = CodingToolOwner.admit

    def observed_admission(owner, round_number, call):
        admit(owner, round_number, call)
        admissions.append((round_number, call.call_id))

    monkeypatch.setattr(provider, "response_chunks", response_chunks)
    monkeypatch.setattr(TrackedTurnSession, "message_end", delayed_declaration)
    monkeypatch.setattr(CodingToolSocket, "handle_request", observed_request)
    monkeypatch.setattr(CodingToolOwner, "admit", observed_admission)
    async with native.open_owner(runtime_enabled=True, auto_wake=True, native_options=()) as (owner, session):
        comms = owner._comms
        comms.threads.claim_thread("verifier", tags=frozenset(), worktree=str(native.project))
        comms.messaging.send_message("verifier", session, "Distinct isolated delayed-round verification.")
        await owner.inputs.drain_inbox(session)
        await asyncio.wait_for(reply_turn_finished(owner, session), 45)

        assert release.is_set()
        assert provider.posts == 3
        assert requests == ["fresh-round-1", "fresh-round-2"]
        assert admissions == [(1, "fresh-round-2")]
        assert effect.read_text() == "fresh-effect\n"
        assert len(sockets) == 1
        sockets[0].assert_complete()
        assert [round.number for round in sockets[0].calls.rounds] == [0, 1]
        assert native.session.read_bytes().startswith(original)
        rows = [json.loads(line) for line in native.session.read_text().splitlines()]
        messages = [row["message"] for row in rows if row.get("type") == "message"]
        assert len([message for message in messages if message.get("role") == "user"]) == 2
        tools = [message for message in messages if message.get("role") == "toolResult"]
        assert [message["toolCallId"] for message in tools] == requests
        assert tools[0]["isError"] is True and not tools[1].get("isError")
        assert "within 10 s" in tools[0]["content"][0]["text"]
        errors = [message for message in messages if message.get("stopReason") == "error"]
        assert len(errors) == 1
        assert provider.error_message in errors[0]["errorMessage"]
        failures = [json.loads(path.read_text()) for path in (native.root / "diagnostics").glob("*.json")]
        assert len(failures) == 1
        assert failures[0]["reason"] == "model_request_failed"
        assert "NativePiUnavailable" not in failures[0].get("source_error", "")
        print("installed_native_round_result=" + json.dumps({
            "installed_module": str(Path(__import__("agent_comms").__file__).resolve()),
            "new_inputs": 1, "tool_calls": requests, "owner_admissions": admissions,
            "effect_lines": 1, "failure_reason": failures[0]["reason"],
            "original_fixture_history_preserved": True,
        }))
