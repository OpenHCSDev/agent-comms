"""One fresh installed native journey covers delayed rounds and error settlement."""

from agent_comms.messages import NoticeRole
import asyncio
import json
from pathlib import Path

import pytest

from agent_comms.channel_coding_tools import CodingToolOwner, CodingToolSocket
from agent_comms.pi_events import MessageEnd, Response
from agent_comms.pi_payloads import ToolCallContent
from agent_comms.native_pi import NativePiTerminalFailure
from agent_comms.turn_failure import ModelRequestFailed
from agent_comms.tracked_turn import TrackedTurnSession
from delivery_owner_fixture import reply_turn_finished


async def test_actual_native_delayed_round_then_tool_effect_and_error(native_backend, monkeypatch):
    native = native_backend
    await native.author_history()
    original = native.session.read_bytes()
    (native.project / "independent-read.txt").write_text("Fresh fixture, not a retained input\n")
    effect = native.project / "independent-effect.txt"
    release = asyncio.Event()
    requests, claims, sockets, terminal_failures = [], [], [], []
    provider = native.provider
    provider.error_type = "invalid_request_error"

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
            provider.error_message = "controlled terminal after the distinct authorized effect"

    apply_native_event = TrackedTurnSession.apply_native_event

    async def delayed_declaration(turn, event):
        if isinstance(event, MessageEnd) and any(
                isinstance(item, ToolCallContent) and item.id == "fresh-round-1"
                for item in event.message.content):
            # The real socket request arrives while stdout is still applying
            # the previous declaration. Withhold that declaration past the
            # deleted ten-second timer; only its actual start may authorize it.
            await asyncio.wait_for(release.wait(), 20)
            await asyncio.sleep(11)
        return await apply_native_event(turn, event)

    handle_request = CodingToolSocket.handle_request

    async def observed_request(socket, raw):
        call_id = json.loads(raw)["call_id"]
        requests.append(call_id)
        if socket not in sockets:
            sockets.append(socket)
        if call_id == "fresh-round-1":
            asyncio.get_running_loop().call_soon(release.set)
        return await handle_request(socket, raw)

    allow = CodingToolOwner.allow

    async def observed_claim(owner, turn, round_number, call):
        reservation = await allow(owner, turn, round_number, call)
        claims.append((round_number, call.call_id))
        return reservation

    result = TrackedTurnSession.result

    async def observed_result(turn):
        try:
            return await result(turn)
        except NativePiTerminalFailure as error:
            terminal_failures.append(error.failure)
            raise

    monkeypatch.setattr(TrackedTurnSession, "result", observed_result)
    monkeypatch.setattr(provider, "response_chunks", response_chunks)
    monkeypatch.setattr(TrackedTurnSession, "apply_native_event", delayed_declaration)
    monkeypatch.setattr(CodingToolSocket, "handle_request", observed_request)
    monkeypatch.setattr(CodingToolOwner, "allow", observed_claim)
    async with native.open_owner(runtime_enabled=True, auto_wake=True, native_options=()) as (owner, session):
        comms = owner._comms
        comms.threads.claim_thread("verifier", tags=frozenset(), worktree=str(native.project))
        comms.messaging.send(
            "verifier", session, f"Distinct isolated delayed-round verification {native.session.stem}."
        ).message
        await owner.inputs.drain_inbox(session)
        with pytest.raises(NativePiTerminalFailure, match=provider.error_message):
            await asyncio.wait_for(reply_turn_finished(owner, session), 120)

        rows = [json.loads(line) for line in native.session.read_text().splitlines()]
        tools = [row["message"] for row in rows if row.get("type") == "message"
                 and row["message"].get("role") == "toolResult"]
        print("delayed_native_start=" + json.dumps({"claims":claims, "tools":tools}))
        assert release.is_set()
        assert provider.posts == 3
        assert requests == ["fresh-round-1", "fresh-round-2"]
        assert claims == [(0, "fresh-round-1"), (1, "fresh-round-2")]
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
        assert not tools[0].get("isError") and not tools[1].get("isError")
        errors = [message for message in messages if message.get("stopReason") == "error"]
        assert len(errors) == 1
        assert provider.error_message in errors[0]["errorMessage"]
        assert len(terminal_failures) == 1
        assert type(terminal_failures[0]) is ModelRequestFailed
        assert provider.error_message in terminal_failures[0].text
        tracked_users = [message for message in messages if message.get("inputId")]
        assert len(tracked_users) == 1
        print("installed_native_round_result=" + json.dumps({
            "installed_module": str(Path(__import__("agent_comms").__file__).resolve()),
            "new_inputs": 1, "tool_calls": requests, "owner_claims": claims,
            "effect_lines": 1, "failure_reason": str(terminal_failures[0].code),
            "original_fixture_history_preserved": True,
        }))


