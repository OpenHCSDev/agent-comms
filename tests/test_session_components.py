"""Session ownership, declaration extension and actual ACP boundary contracts."""

import json
from dataclasses import dataclass, replace

import pytest
from acp.agent.router import build_agent_router
from acp.schema import SessionConfigSelectOption, TextContentBlock, UserMessageChunk

from agent_comms import backend, wire
from agent_comms.acp import CommsAgent, CommsClient
from agent_comms.config_options import ConfigOption
from agent_comms.declarations import MessageRoute
from agent_comms.operations import TranscriptEvent
from agent_comms.session_lifecycle import AttachedSessionLifecycle, SessionLifecycle
from agent_comms.transcript_updates import (
    AssistantTranscriptUpdate,
    NoticeTranscriptUpdate,
    StartedTranscriptUpdate,
    TranscriptUpdate,
    UserTranscriptUpdate,
)


@pytest.fixture
async def owner(tmp_path, monkeypatch):
    async def models(*args):
        return [backend.Model("test/one", "One"), backend.Model("test/two", "Two")]

    async def levels(*args):
        return ["low", "medium", "high"]

    monkeypatch.setattr(backend, "discover_models", models)
    monkeypatch.setattr(backend, "discover_thinking_levels", levels)
    agent = CommsAgent(
        wire(tmp_path / "wire"),
        agent_bin="/bin/echo",
        agent_args=["--model", "test/one"],
        auto_wake=False,
    )
    monkeypatch.setattr(agent.inputs, "ensure_live_drain", lambda _: None)
    try:
        yield agent
    finally:
        await agent.shutdown()


async def test_state_is_owned_once_and_attachments_do_not_share_negotiation(owner, tmp_path):
    other = CommsClient(wire(tmp_path / "other"), agent_bin="/bin/echo")
    assert type(owner.sessions) is SessionLifecycle
    assert type(other.sessions) is AttachedSessionLifecycle
    await owner.initialize(
        1, {"_meta": {"agentComms": {"transcriptSnapshots": True, "transcriptDiffs": True}}}
    )
    await other.initialize(1, {})
    session = await owner.new_session(str(tmp_path / "project"))
    assert (
        owner.sessions.config.session_catalog_generation[session.session_id]
        == owner.sessions.config.catalog_generation
    )
    assert owner.sessions.transcript.snapshots is True and owner.sessions.transcript.diffs is True
    assert not other.sessions.transcript.snapshots and not other.sessions.bindings
    assert not {
        "_sessions",
        "_proxies",
        "_client",
        "_model_catalog",
        "_catalog_generation",
        "_setting_requests",
        "_transcript_snapshots",
    }.intersection(vars(owner))
    await other.shutdown()


async def test_one_option_declaration_reaches_real_acp_router_and_persistence(
    owner, tmp_path, monkeypatch
):
    monkeypatch.setattr(ConfigOption, "__registry__", dict(ConfigOption.__registry__))

    class TaskNoteConfigOption(ConfigOption):
        title = "Task note"
        description = "Declaration extension fixture"
        category = "thought_level"

        @classmethod
        def current_value(cls, thread):
            return thread.task or "medium"

        @classmethod
        async def describe(cls, config, thread):
            return cls.select(
                cls.current_value(thread),
                [
                    SessionConfigSelectOption(value=value, name=value.title())
                    for value in ("medium", "high")
                ],
            )

        @classmethod
        async def change(cls, config, session_id, thread, value):
            config.comms.registry.register(replace(thread, task=value))

    router = build_agent_router(owner)
    response = await router(
        "session/new", {"cwd": str(tmp_path / "project"), "mcpServers": []}, False
    )
    assert [option.id for option in response.config_options] == [
        "model",
        "thinking_level",
        "task_note",
    ]
    changed = await router(
        "session/set_config_option",
        {"sessionId": response.session_id, "configId": "task_note", "value": "high"},
        False,
    )
    assert changed["configOptions"][-1]["currentValue"] == "high"
    assert wire(owner._comms.root).registry.require(response.session_id).task == "high"

    # Polling must use the same declaration catalog as parser/set dispatch.
    # A newly declared option backed by another Thread field is not invisible.
    updates = []

    class Client:
        async def session_update(self, session_id, update):
            updates.append(update)

    owner.on_connect(Client())
    await owner.sessions.config.sync_thread(response.session_id)
    updates.clear()
    thread = owner._comms.registry.require(response.session_id)
    owner._comms.registry.register(replace(thread, task="medium"))
    await owner.sessions.config.sync_thread(response.session_id)
    assert len(updates) == 1
    assert updates[0].config_options[-1].current_value == "medium"


async def test_a_new_saved_update_is_decoded_and_published_without_consumer_edits(
    owner, monkeypatch
):
    monkeypatch.setattr(TranscriptUpdate, "__registry__", dict(TranscriptUpdate.__registry__))
    updates = []

    @dataclass(frozen=True, kw_only=True)
    class HighlightTranscriptUpdate(TranscriptUpdate):
        text: str

        async def publish(self, session_id, client):
            await client.session_update(
                session_id=session_id,
                update=UserMessageChunk(
                    session_update="user_message_chunk",
                    content=TextContentBlock(type="text", text="Highlight: " + self.text),
                ),
            )

    class Client:
        async def session_update(self, **kwargs):
            updates.append(kwargs["update"])

    await owner._emit_event(
        "alias",
        TranscriptUpdate.from_transcript(TranscriptEvent(kind="highlight", text="saved text")),
        client=Client(),
    )
    assert len(updates) == 1 and updates[0].content.text == "Highlight: saved text"


async def test_typed_updates_preserve_protocol_json_and_unknown_saved_kind(owner):
    rows = []

    class Client:
        async def session_update(self, session_id, update):
            rows.append(json.loads(update.model_dump_json(by_alias=True, exclude_none=True)))

    client = Client()
    for event in [
        UserTranscriptUpdate(text="hello"),
        AssistantTranscriptUpdate(text="answer", route=MessageRoute("a", ("b",))),
        NoticeTranscriptUpdate(text="saved notice"),
        StartedTranscriptUpdate(
            turn_id="turn", started_at=12.0, activity="working", activity_detail="read"
        ),
        TranscriptUpdate.from_transcript(TranscriptEvent(kind="tool_start", text="unrendered")),
    ]:
        await owner._emit_event("session", event, client=client)
    assert rows == [
        {"sessionUpdate": "user_message_chunk", "content": {"type": "text", "text": "hello"}},
        {
            "sessionUpdate": "agent_message_chunk",
            "content": {"type": "text", "text": "answer"},
            "_meta": {"agentComms": {"route": {"sender": "a", "targets": ["b"]}}},
        },
        {
            "sessionUpdate": "agent_message_chunk",
            "content": {"type": "text", "text": "saved notice"},
            "_meta": {"agentComms": {"route": None}},
        },
        {
            "sessionUpdate": "agent_message_chunk",
            "content": {"type": "text", "text": ""},
            "_meta": {
                "agentComms": {
                    "turnStarted": True,
                    "turnId": "turn",
                    "startedAt": 12.0,
                    "activity": "working",
                    "activityDetail": "read",
                }
            },
        },
    ]
