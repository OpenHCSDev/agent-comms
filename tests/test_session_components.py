"""Session ownership, declaration extension and actual ACP boundary contracts."""

import os
from dataclasses import replace

import pytest
from agent_comms.pi_vocabulary import NoThinkingOverride, OffThinkingLevel
from acp.agent.router import build_agent_router
from acp.schema import SessionConfigSelectOption

from agent_comms.thread_identity import RecordedThreadIncarnation
from agent_comms.acp import CommsClient
from agent_comms.child_process import NoProcess, ProcessIdentity
from agent_comms.comms import Comms
from agent_comms.coordinator import Coordination
from agent_comms.errors import RelationViolationError
from agent_comms.config_options import (
    CatalogConfigOption,
    ConfigOption,
    ModelConfigOption,
    ThinkingLevelConfigOption,
)
from agent_comms.session_lifecycle import AttachedSessionLifecycle, SessionLifecycle
from agent_comms import agent_events as events
from agent_comms.pi_vocabulary import OffThinkingLevel, HighThinkingLevel
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
    monkeypatch.delenv("AGENT_COMMS_THREAD", raising=False)
    agent = canonical_agent(
        Comms(tmp_path / "wire"),
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
    other = CommsClient(Comms(tmp_path / "other"), agent_bin="pi")
    assert type(owner.sessions) is SessionLifecycle
    assert type(other.sessions) is AttachedSessionLifecycle
    await owner.initialize(1, {})
    await other.initialize(1, {})
    session = await owner.new_session(str(tmp_path / "project"))
    thread = owner._comms.registry.require(session.session_id)
    assert thread.process_identity == ProcessIdentity.capture(os.getpid())
    with Coordination(str(owner._comms.root / "coordination.sqlite3")) as store:
        participant = store.participants.get(RecordedThreadIncarnation("fixture", thread.created_at).lookup)
    assert participant.committed and participant.owner_thread == thread.name
    assert participant.pointer.execution_id is None
    assert session.session_id not in owner.sessions.config.publications
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


async def test_attached_declaration_publishes_membership_without_acquiring_executor(owner, tmp_path):
    attachment = CommsClient(owner._comms, agent_bin="pi", auto_wake=False)
    try:
        thread = await Coordination.run_worker(
            lambda: attachment.sessions.declare_thread(str(tmp_path / "attached"))
        )
        assert type(attachment.sessions) is AttachedSessionLifecycle
        assert thread.process_identity == NoProcess()
        assert owner._comms.registry.require(thread.name) == thread
        assert owner._comms.registry.status(thread.name).active
        assert not attachment.sessions.bindings and not attachment.sessions.proxies
        with Coordination(str(owner._comms.root / "coordination.sqlite3")) as store:
            participant = store.participants.get(RecordedThreadIncarnation("fixture", thread.created_at).lookup)
        assert participant.committed and participant.owner_thread == thread.name
        assert participant.pointer.execution_id is None
    finally:
        await attachment.shutdown()


def test_thread_service_tier_round_trips_legacy_rows(*, thread_registration):
    from agent_comms.field_codec import FieldCodec
    from agent_comms.pi_vocabulary import ServiceTier
    from agent_comms.threads import Thread

    encoded = FieldCodec.encode(thread_registration.new_thread('legacy', frozenset(), '.'))
    assert encoded.pop("service_tier") == "priority"
    restored = FieldCodec.decode(Thread, encoded)
    assert restored.service_tier is ServiceTier.PRIORITY
    standard = thread_registration.new_thread('standard', frozenset(), '.', service_tier=ServiceTier.STANDARD)
    assert FieldCodec.decode(Thread, FieldCodec.encode(standard)).service_tier is ServiceTier.STANDARD
    with pytest.raises(RelationViolationError, match="configuration changed"):
        standard.require_original_model_configuration(restored)


async def test_native_observation_only_initializes_unset_configuration(owner, tmp_path):
    session = await owner.new_session(str(tmp_path / "native-config"))
    thread = owner._comms.registry.require(session.session_id)
    assert thread.thinking_level is NoThinkingOverride
    assert thread.service_tier.value == "priority"
    await owner.sessions.observe_native_configuration(
        session.session_id, thread.name, events.AgentInfo(
            model="test/two", thinking_level=HighThinkingLevel
        )
    )
    observed = owner._comms.registry.require(thread.name)
    assert observed.model == "test/one"
    assert observed.thinking_level is HighThinkingLevel
    environment = observed.native_environment(
        owner._comms.root, owner._comms.registry.snapshot(), observed.worktree
    )
    assert "AGENT_COMMS_SERVICE_TIER" not in environment
    assert owner._comms.views.thread_detail(thread.name, include_pending=False).to_wire()["service_tier"] == "priority"
    owner._comms.threads.set_thread_configuration(owner._comms.registry.require(thread.name), thinking_level=OffThinkingLevel)
    before = (owner._comms.root / "registry.json").read_bytes()
    await owner.sessions.observe_native_configuration(
        session.session_id, thread.name, events.AgentInfo(
            model="test/two", thinking_level=HighThinkingLevel
        )
    )
    assert owner._comms.registry.require(thread.name).thinking_level is OffThinkingLevel
    assert (owner._comms.root / "registry.json").read_bytes() == before


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

        async def apply(self, config, thread, value):
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
    assert Comms(owner._comms.root).registry.require(response.session_id).task == "high"

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
