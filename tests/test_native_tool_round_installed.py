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
        reports = [json.loads(path.read_text()) for path in (native.root / "diagnostics").glob("*.json")]
        failures = [report for report in reports if report.get("turn_id") == tracked_users[0]["inputId"]]
        assert len(failures) == 1
        assert "NativePiTerminalFailure" in failures[0]["source_error"]
        assert "without matching durable proof" not in failures[0]["source_error"]
        print("installed_native_round_result=" + json.dumps({
            "installed_module": str(Path(__import__("agent_comms").__file__).resolve()),
            "new_inputs": 1, "tool_calls": requests, "owner_claims": claims,
            "effect_lines": 1, "failure_reason": str(terminal_failures[0].code),
            "original_fixture_history_preserved": True,
        }))


@pytest.mark.parametrize("following", [
    "empty_notice", "answered_notice", "steering", "provider_error", "empty_original",
    "empty_original_notice", "tool_then_notice",
])
async def test_actual_native_requested_answer_and_following_input(native_backend, monkeypatch, following):
    from acp.schema import TextContentBlock
    import agent_comms.pi_commands as pi_commands
    from agent_comms.native_pi import NativePiUnavailable
    from agent_comms.wire_record import WireRecord

    native = native_backend
    await native.author_history()
    original = native.session.read_bytes()
    gate = asyncio.Event()
    native.provider.response_gate = None if following == "empty_original" else gate
    native.provider.error_type = "invalid_request_error"
    native.provider.error_message = "controlled genuine following provider failure"
    failures, acknowledgments = [], []

    def chunks():
        assert native.provider.posts <= (3 if following == "tool_then_notice" else 2), "Clean stops must not generate provider retries"
        if following == "tool_then_notice" and native.provider.posts == 1:
            yield {"tool_calls": [{"index": 0, "id": "fresh-original-tool", "type": "function",
                "function": {"name": "bash", "arguments": json.dumps({
                    "command": "printf 'one-effect\\n' >> fresh-original-effect.txt"})}}]}, None
            yield {}, "tool_calls"
            return
        primary = native.provider.posts == (2 if following == "tool_then_notice" else 1)
        text = ("" if following in {"empty_original", "empty_original_notice"} else "Requested answer") if primary else {
            "empty_notice": "", "answered_notice": '{"decision":"IGNORE"}',
            "steering": "Revised requested answer", "empty_original_notice": '{"decision":"IGNORE"}',
            "tool_then_notice": '{"decision":"IGNORE"}',
        }[following]
        yield {"content": text}, None
        yield {}, "stop"
        if following == "provider_error":
            native.provider.status = 400

    committed_input = TrackedTurnSession.committed_input
    apply_native_event = TrackedTurnSession.apply_native_event
    result = TrackedTurnSession.result

    async def original_committed(turn, event):
        await committed_input(turn, event)
        if event.input_id != turn.command.input_id or following == "empty_original":
            return
        if following == "steering":
            await owner.prompt(session, [TextContentBlock(type="text", text="Fresh real user correction")])
        else:
            comms.messaging.send("verifier", session, "Fresh background notice; no automatic reply requested").message
            await owner.inputs.drain_inbox(session)

    async def acknowledged(turn, event):
        updates = await apply_native_event(turn, event)
        if (isinstance(event, Response) and event.success and event.command is pi_commands.Prompt
                and event.id != turn.command.id):
            acknowledgments.append(event.id)
            gate.set()  # The real native RPC has queued the following input before the answer completes.
        return updates

    async def terminal(turn):
        try:
            return await result(turn)
        except NativePiUnavailable as error:
            failures.append(error)
            raise

    monkeypatch.setattr(native.provider, "response_chunks", chunks)
    monkeypatch.setattr(TrackedTurnSession, "committed_input", original_committed)
    monkeypatch.setattr(TrackedTurnSession, "apply_native_event", acknowledged)
    monkeypatch.setattr(TrackedTurnSession, "result", terminal)
    async with native.open_owner(runtime_enabled=True, auto_wake=True, native_options=()) as (owner, session):
        comms = owner._comms
        comms.threads.claim_thread("verifier", tags=frozenset(), worktree=str(native.project))
        comms.messaging.send("verifier", session, "Fresh requested-answer lifecycle verification").message
        await owner.inputs.drain_inbox(session)
        await asyncio.wait_for(reply_turn_finished(owner, session), 30)
        assert native.provider.posts == (1 if following == "empty_original" else 3 if following == "tool_then_notice" else 2)
        if following == "tool_then_notice":
            assert (native.project / "fresh-original-effect.txt").read_text() == "one-effect\n"
        assert len(acknowledgments) == (0 if following == "empty_original" else 1)
        assert native.session.read_bytes().startswith(original)
        messages = [row["message"] for line in native.session.read_text().splitlines()
                    if (row := json.loads(line)).get("type") == "message"]
        users = [message for message in messages if message.get("inputId")]
        assert len(users) == (1 if following == "empty_original" else 2)
        assert len({message["inputId"] for message in users}) == len(users)
        replies = [message for line in comms.bus.log.path.read_text().splitlines()
                   for message in WireRecord.public_from_wire(json.loads(line)).messages()
                   if message.sender == session]
        notices = [message for message in replies if isinstance(message.role, NoticeRole)]
        replies = [message for message in replies if not isinstance(message.role, NoticeRole)]
        if following == "provider_error":
            assert not replies and len(notices) == len(failures) == 1
            assert isinstance(failures[0], NativePiTerminalFailure)
            assert isinstance(failures[0].failure, ModelRequestFailed)
            assert native.provider.error_message in str(failures[0])
        elif following in {"empty_original", "empty_original_notice"}:
            assert not replies and len(notices) == len(failures) == 1
            assert "no unique authoritative completed response" in str(failures[0])
        else:
            assert not failures and not notices
            assert [message.body for message in replies] == [
                "Revised requested answer" if following == "steering" else "Requested answer"]
        print("installed_native_following_result=" + json.dumps({
            "following": following, "provider_requests": native.provider.posts,
            "new_inputs": len(users), "routed_replies": [message.body for message in replies],
            "failure_notices": len(notices),
            "original_fixture_history_preserved": True,
            "installed_module": str(Path(__import__("agent_comms").__file__).resolve()),
        }))
