"""Real native coding and Comms execution share the existing checked call lifecycle."""

import asyncio
import json
from dataclasses import replace
from pathlib import Path

from agent_comms.channel_coding_tools import CodingToolOwner
from agent_comms.field_codec import FieldCodec
from agent_comms.goal_history import Before, GoalHistoryQuery, OneGoal
from agent_comms.goal_states import PausedGoal
from agent_comms.goals import Goal
from agent_comms.wire_record import WireRecord
from delivery_owner_fixture import reply_turn_finished
from test_backend_native_lifecycle import native_backend


async def test_native_send_and_bash_share_checked_execution(native_backend, monkeypatch):
    native = native_backend
    await native.author_history()
    original = native.session.read_bytes()
    admissions = []
    admit = CodingToolOwner.admit

    async def admitted(owner, round_number, call):
        await admit(owner, round_number, call)
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


async def test_native_goal_history_uses_declared_nested_selection(native_backend, monkeypatch):
    native = native_backend
    await native.author_history()
    original = native.session.read_bytes()
    async with native.open_owner(runtime_enabled=True, auto_wake=True, native_options=()) as (owner, session):
        comms = owner._comms
        comms.threads.claim_thread("requester", tags=frozenset(), worktree=str(native.project))
        source = comms.registry.require(session)
        goal = Goal("Authored paused history", "native-history", state=PausedGoal())
        comms.registry.register(replace(source, goal=goal))
        retained = comms.goals.goal_history(session)
        query = GoalHistoryQuery(scope=OneGoal(goal.id), cursor=Before(retained[0].sequence + 1), limit=1)

        def chunks():
            number = native.provider.posts
            assert number <= 2, "No input or tool replay is allowed"
            if number == 2:
                yield {"content": "Native nested goal history complete"}, None
                yield {}, "stop"
                return
            tool = next(tool["function"] for tool in native.provider.requests[-1]["tools"]
                        if tool["function"]["name"] == "comms_goal_history")
            properties = tool["parameters"]["properties"]
            assert {"scope", "cursor", "limit"} <= properties.keys()
            assert not {"goal_id", "before_sequence"} & properties.keys()
            yield {"tool_calls": [{"index": 0, "id": "fresh-goal-history", "type": "function",
                "function": {"name": "comms_goal_history", "arguments": json.dumps(FieldCodec.encode(query))}}]}, None
            yield {}, "tool_calls"

        monkeypatch.setattr(native.provider, "response_chunks", chunks)
        comms.messaging.send_message("requester", session, "Read the authored retained history once")
        await owner.inputs.drain_inbox(session)
        await asyncio.wait_for(reply_turn_finished(owner, session), 35)
        assert native.provider.posts == 2
        assert native.session.read_bytes().startswith(original)
        saved = [row["message"] for line in native.session.read_text().splitlines()
                 if (row := json.loads(line)).get("type") == "message"]
        assert len([message for message in saved if message.get("inputId")]) == 1
        tools = [message for message in saved if message.get("role") == "toolResult"]
        assert len(tools) == 1 and tools[0]["toolName"] == "comms_goal_history"
        assert not tools[0].get("isError")
        assert json.loads(tools[0]["content"][0]["text"]) == {
            "history": [retained[0].to_wire()],
        }
        assert not list((native.root / "diagnostics").glob("*.json"))
        print("native_goal_history_result=" + json.dumps({
            "installed_module": str(Path(__import__("agent_comms").__file__).resolve()),
            "new_inputs": 1, "native_tool_calls": 1, "provider_requests": 2,
            "original_fixture_history_preserved": True, "nested_scope_and_cursor": True,
        }))
