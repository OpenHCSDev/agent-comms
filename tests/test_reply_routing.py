from bus_history import read_history
from agent_comms.channel_targets import CommandTarget
import asyncio
import json

import pytest

import agent_comms.agent_events as ae
from agent_comms.tools import invoke_tool
from agent_comms.acp_extension import QueuePromptRequest, encode_request
from agent_comms.input_origin import HumanInputOrigin
from delivery_owner_fixture import canonical_agent
from agent_comms.comms import wire
from agent_comms.routing import MessageRoute
from agent_comms.threads import Thread


def test_incoming_route_distinguishes_channel_and_direct_delivery():
    assert MessageRoute("user", ("#experiment",)).incoming_scope == "#experiment"
    assert MessageRoute("user", ("worker",)).incoming_scope == "direct message"


async def test_native_user_dm_reply_ignores_earlier_channel_route(
    wired, tmp_path, monkeypatch
):
    monkeypatch.setenv("AGENT_COMMS_AGENT_MODELS", "test/model")
    project = tmp_path / "worker"
    project.mkdir()
    comms = wired
    agent = canonical_agent(
        comms, agent_bin="pi", agent_args=[], runtime_enabled=True, auto_wake=False
    )
    session = await agent.new_session(str(project))

    class Client:
        async def session_update(self, **kwargs):
            pass

    agent.sessions.client = Client()
    session_file = tmp_path / "session.jsonl"
    session_file.touch()
    comms.threads.attach_session((attachment_owner := comms.registry.require("worker")), str(session_file), process=attachment_owner.process_identity)
    comms.channels.update_tags("worker", add=frozenset({"test"}))
    human = comms.messaging.user_identity(str(project))
    origin = HumanInputOrigin.capture(
        comms, comms.registry.snapshot().claimed_identity("worker")
    )
    earlier = comms.messaging.send_user_message(
        "#test", "Earlier channel context", worktree=str(project)
    )
    channel_context = read_history(comms, "#test")
    assert channel_context[-1] == earlier
    prompts: list[str] = []

    async def events(*args, **kwargs):
        prompts.append(args[2])
        native_id = "a" * 32
        with kwargs["send_boundary"](None, native_id, args[2]) as allowed:
            assert allowed
        with session_file.open("a") as output:
            output.write(json.dumps({
                "type": "message",
                "id": "user-input",
                "message": {
                    "role": "user",
                    "inputId": native_id,
                    "content": args[2],
                },
            }) + "\n")
        assert kwargs["native_start"](None, native_id, args[2])
        yield ae.OriginalInputStarted()
        yield ae.Chunk(text="Private DM answer")
        yield ae.StreamSettled()
        yield ae.Done(ok=True, text="")

    monkeypatch.setattr("agent_comms.backend.stream_agent_events", events)
    try:
        response = await agent.prompt(
            session_id=session.session_id,
            prompt=[{"type": "text", "text": "Unrelated private question"}],
            **encode_request(QueuePromptRequest(origin=origin)),
        )
        assert response.stop_reason == "end_turn"
        assert len(prompts) == 1
        assert "Unrelated private question" in prompts[0]
        assert f"delivered automatically to {human.name}" in prompts[0]
        assert read_history(comms, "#test") == channel_context
        assert list(comms.views.history_page(CommandTarget.named(human.name), viewer="worker").messages) == []
    finally:
        await agent.shutdown()


