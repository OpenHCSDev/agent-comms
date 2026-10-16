"""Real native coding and Comms execution share the existing checked call lifecycle."""

import asyncio
import json
from pathlib import Path

from agent_comms.channel_coding_tools import CodingToolOwner
from agent_comms.wire_record import WireRecord
from delivery_owner_fixture import reply_turn_finished
from test_backend_native_lifecycle import native_backend


async def test_native_send_and_bash_share_checked_execution(native_backend, monkeypatch):
    native = native_backend
    await native.author_history()
    original = native.session.read_bytes()
    admissions = []
    admit = CodingToolOwner.admit

    def admitted(owner, round_number, call):
        admit(owner, round_number, call)
        admissions.append((round_number, call.name))

    monkeypatch.setattr(CodingToolOwner, "admit", admitted)
    async with native.open_owner(runtime_enabled=True, auto_wake=True, native_options=()) as (owner, session):
        comms = owner._comms
        for name in ("requester", "receiver"):
            comms.threads.claim_thread(name, tags=frozenset(), worktree=str(native.project))

        def chunks():
            number = native.provider.posts
            assert number <= 3, "No input or effect replay is allowed"
            if number == 3:
                yield {"content": "Fresh Send and coding complete"}, None
                yield {}, "stop"
                return
            name, arguments = (
                ("comms_send", {"from": session, "to": "receiver", "body": "One fresh tool message"})
                if number == 1 else
                ("bash", {"command": "printf 'one-code-effect\\n' >> native-code-effect.txt"})
            )
            supplied = {tool["function"]["name"] for tool in native.provider.requests[-1]["tools"]}
            assert {"read", "bash", "edit", "write", "comms_send"} <= supplied
            yield {"tool_calls": [{"index": 0, "id": f"fresh-tool-{number}", "type": "function",
                "function": {"name": name, "arguments": json.dumps(arguments)}}]}, None
            yield {}, "tool_calls"

        monkeypatch.setattr(native.provider, "response_chunks", chunks)
        comms.messaging.send_message("requester", session, "Fresh isolated Send and coding execution verification")
        await owner.inputs.drain_inbox(session)
        await asyncio.wait_for(reply_turn_finished(owner, session), 35)
        assert native.provider.posts == 3
        assert admissions == [(0, "comms_send"), (1, "bash")]
        assert (native.project / "native-code-effect.txt").read_text() == "one-code-effect\n"
        assert native.session.read_bytes().startswith(original)
        messages = [message for line in comms.bus.log.path.read_text().splitlines()
                    for message in WireRecord.public_from_wire(json.loads(line)).messages()
                    if message.sender == session]
        assert [(message.target, message.body) for message in messages] == [
            ("receiver", "One fresh tool message"), ("requester", "Fresh Send and coding complete")]
        saved = [row["message"] for line in native.session.read_text().splitlines()
                 if (row := json.loads(line)).get("type") == "message"]
        users = [message for message in saved if message.get("inputId")]
        tools = [message for message in saved if message.get("role") == "toolResult"]
        assert len(users) == 1
        assert [message["toolName"] for message in tools] == ["comms_send", "bash"]
        assert all(not message.get("isError") for message in tools)
        assert not list((native.root / "diagnostics").glob("*.json"))
        print("native_comms_coding_result=" + json.dumps({
            "installed_module": str(Path(__import__("agent_comms").__file__).resolve()),
            "new_inputs": 1, "owner_checks": admissions, "provider_requests": 3,
            "private_messages": 1, "coding_effects": 1, "routed_replies": 1,
            "original_fixture_history_preserved": True,
        }))
