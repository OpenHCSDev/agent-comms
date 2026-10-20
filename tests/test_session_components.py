"""Session ownership, declaration extension and actual ACP boundary contracts."""

import os
from dataclasses import replace

import pytest
from agent_comms.pi_vocabulary import NoThinkingOverride, OffThinkingLevel
from acp.agent.router import build_agent_router
from acp.schema import SessionConfigSelectOption

from agent_comms.thread_identity import RecordedThreadIncarnation
from agent_comms.acp import CommsClient
from agent_comms.child_process import ProcessIdentity
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
import agent_comms.agent_events as events
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
        assert thread.pid == 0 and thread.process_identity is None
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


