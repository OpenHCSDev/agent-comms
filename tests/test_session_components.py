"""Session ownership, declaration extension and actual ACP boundary contracts."""

from dataclasses import replace

import pytest
from acp.agent.router import build_agent_router
from acp.schema import SessionConfigSelectOption

from agent_comms.acp import CommsClient
from agent_comms.comms import wire
from agent_comms.config_options import (
    CatalogConfigOption,
    ConfigOption,
    ModelConfigOption,
    ThinkingLevelConfigOption,
)
from agent_comms.session_lifecycle import AttachedSessionLifecycle, SessionLifecycle
from delivery_owner_fixture import canonical_agent


@pytest.fixture
async def owner(tmp_path, monkeypatch):
    async def models(*args):
        return [
            SessionConfigSelectOption(value="test/one", name="One"),
            SessionConfigSelectOption(value="test/two", name="Two"),
        ]

    async def levels(*args):
        return [
            SessionConfigSelectOption(value=v, name=v.title()) for v in ("low", "medium", "high")
        ]

    monkeypatch.setattr(ModelConfigOption, "discover", models)
    monkeypatch.setattr(ThinkingLevelConfigOption, "discover", levels)
    agent = canonical_agent(
        wire(tmp_path / "wire"),
        agent_bin="pi",
        agent_args=["--model", "test/one"],
        auto_wake=False,
    )
    monkeypatch.setattr(agent.inputs, "ensure_live_drain", lambda _: None)
    try:
        yield agent
    finally:
        await agent.shutdown()


async def test_state_is_owned_once_and_attachments_do_not_share_negotiation(owner, tmp_path):
    other = CommsClient(wire(tmp_path / "other"), agent_bin="pi")
    assert type(owner.sessions) is SessionLifecycle
    assert type(other.sessions) is AttachedSessionLifecycle
    await owner.initialize(1, {})
    await other.initialize(1, {})
    session = await owner.new_session(str(tmp_path / "project"))
    assert (
        owner.sessions.config.session_catalog_generation[session.session_id]
        == owner.sessions.config.catalog_generation
    )
    assert not other.sessions.bindings
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

    class TaskNoteConfigOption(CatalogConfigOption):
        title = "Task note"
        description = "Declaration extension fixture"
        category = "thought_level"

        def current_value(self, thread):
            return thread.task or "medium"

        async def discover(self, thread):
            return [
                SessionConfigSelectOption(value=value, name=value.title())
                for value in ("medium", "high")
            ]

        async def apply(self, config, session_id, thread, value):
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
