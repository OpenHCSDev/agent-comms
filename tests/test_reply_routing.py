from bus_history import history
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


async def test_route_is_forwarded_live_and_preserved_by_entry_id(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_COMMS_AGENT_MODELS", "test/model")
    comms = wire(tmp_path / "wire")
    agent = canonical_agent(comms, agent_bin="pi", agent_args=[], runtime_enabled=True, auto_wake=False)
    await agent.new_session(str(tmp_path / "worker"))
    comms.channels.update_tags("worker", add=frozenset({"test"}))
    session = tmp_path / "session.jsonl"
    session.write_text('{"type":"session","id":"session","version":3}\n')
    comms.threads.attach_session((attachment_owner := comms.registry.require("worker")), str(session), process=attachment_owner.process_identity)
    updates = []

    class Client:
        async def session_update(self, **kwargs):
            updates.append(kwargs["update"])

    agent.sessions.client = Client()

    async def events(*args, **kwargs):
        native_id = "a" * 32
        with kwargs["send_boundary"](None, native_id, args[2]) as allowed:
            assert allowed
        assert kwargs["native_start"](None, native_id, args[2])
        with session.open("a") as output:
            for identity, role, content in (
                ("u1", "user", args[2]),
                ("a1", "assistant", "Channel answer"),
            ):
                output.write(
                    json.dumps(
                        {
                            "type": "message",
                            "id": identity,
                            "message": {
                                "role": role,
                                "content": content,
                                **({"inputId": native_id} if role == "user" else {}),
                            },
                        }
                    )
                    + "\n"
                )
        yield ae.Chunk(text="Channel answer")
        yield ae.StreamSettled()
        yield ae.Done(ok=True, text="")

    monkeypatch.setattr("agent_comms.backend.stream_agent_events", events)
    try:
        message = comms.messaging.send_user_message(
            "#test", "Question in channel", worktree=str(tmp_path)
        )
        # Routing persists by entry identity on the current owned turn. Native
        # channel notification/claim_scope is covered by the canonical drain tests.
        await asyncio.wait_for(agent.turns.run_agent_turn(
            "worker", "worker", message.body, origins=(message,),
        ), 2)
        routed = [
            update
            for update in updates
            if (update.field_meta or {}).get("agentComms", {}).get("route")
        ]
        assert routed[0].field_meta["agentComms"]["route"]["targets"] == ("#test",)
        page = wire(tmp_path / "wire").transcripts.thread_transcript_page("worker")
        assert page.events[0].text == "Question in channel"
        assert page.events[0].routing.requests[0].sender == "user"
        assert page.events[-1].routing.reply.outgoing_label == "To #test"
        # Equal private text must not inherit routing from matching content.
        with session.open("a") as output:
            output.write(
                json.dumps(
                    {
                        "type": "message",
                        "id": "private",
                        "message": {"role": "assistant", "content": "Channel answer"},
                    }
                )
                + "\n"
            )
        assert (
            wire(tmp_path / "wire").transcripts.thread_transcript_page("worker").events[-1].routing
            is None
        )
    finally:
        await agent.shutdown()


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
    channel_context = history(comms, "#test")
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
        assert history(comms, "#test") == channel_context
        assert list(comms.views.history_page(CommandTarget.named(human.name), viewer="worker").messages) == []
    finally:
        await agent.shutdown()


async def test_coordination_context_does_not_override_scheduled_response_policy(tmp_path):
    comms = wire(tmp_path / "wire")
    agent = canonical_agent(comms, agent_bin="/bin/echo", agent_args=[], runtime_enabled=True)
    await agent.new_session(str(tmp_path / "worker"))
    seen: dict = {}

    async def events(_bin, _args, task, _worktree, _env, **kwargs):
        seen["task"] = task
        seen.update(kwargs)
        yield ae.StreamSettled()
        yield ae.Done(ok=True, text="")

    import agent_comms.backend as backend_module

    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(backend_module, "stream_agent_events", events)
    try:
        await agent.turns.run_agent_turn("worker", "worker", "incoming broadcast")
    finally:
        monkeypatch.undo()
        await agent.shutdown()
    assert "Response policy:" not in seen["task"]
    assert "mentioned_only" not in seen["task"]
