"""Carry the previous indexed baseline without promoting a disposable HUD to history."""

import time
from dataclasses import replace
from pathlib import Path

import pytest
from cutover.carry_rule_ping_types import RulePingTypes
from cutover.carry_team_observations import TeamObservations
from cutover.carry_team_update_lines import TeamUpdateLines

from agent_comms.comms import Comms
from agent_comms.installer.carry import CarryChain
from agent_comms.messaging.bus import Bus
from agent_comms.nominal.field_codec import FieldCodec
from agent_comms.projects.checkouts import Checkout
from agent_comms.registry.heartbeat_delivery import OnHeartbeatDelivery
from agent_comms.registry.thread_identity import RecordedThreadIncarnation
from agent_comms.registry.threads import NativeThread
from agent_comms.storage.locked_store import LiveFilesRequest
from agent_comms.task_attachments.team_pings import (
    TeamUpdate,
    TeamUpdateNotice,
    TeamUpdateStore,
)
from agent_comms.team.standing_rules import ServicePassFailing
from agent_comms.team.team_hud import CodexUsage, TeamFrame, TeamHud, TeamPlan, UnreadableUsage
from agent_comms.team.vibe_check import OverloadDegradation


@pytest.fixture
def carry_bus(comms: Comms, tmp_path: Path) -> Comms:
    comms.registry.register(NativeThread(
        name="carry-colleague", tags=frozenset(), worktree=Checkout(tmp_path),
        heartbeat_delivery=OnHeartbeatDelivery))
    colleague = comms.registry.require("carry-colleague").incarnation
    now = time.time()
    original = TeamUpdateNotice(colleague, ())
    original.send(comms, ("original quiet publication",))
    TeamFrame.replace_all(comms.root, TeamPlan(), (TeamHud(
        teammate=colleague, units=(), phases=(), forks=(),
        load=OverloadDegradation.from_orders((), 0, 0),
        usage=CodexUsage(UnreadableUsage(reason="carry fixture needs no account")),
        packets=(original,), open_pings=(),
    ),))
    failure = ServicePassFailing(watch="carry-source", since=now - 120,
                                failed_at=now - 60, error="later failure")
    latest, = TeamUpdate.report_failure(
        colleague, failure, (original,), published=(original,), now=now,
    )
    latest.send(comms, ("failure after the older HUD was saved",))
    return comms


def test_carry_preserves_indexed_failure_and_age_without_rewriting_history(
    carry_bus: Comms, tmp_path: Path,
) -> None:
    comms = carry_bus
    colleague = comms.registry.require("carry-colleague").incarnation
    expected = TeamUpdate.published(comms, colleague)
    stale, = TeamFrame.at(comms.root, colleague).read().packets
    assert expected[0].handled and not stale.handled
    store = TeamUpdateStore.for_team(comms.root, colleague)
    step = TeamObservations(LiveFilesRequest(comms.root), tmp_path / "spill")
    before = Bus.digest(Bus.path_in(comms.root))
    assert step.carry(apply=False).changed == 1
    assert not store.path.exists()
    applied = step.carry(apply=True).for_step(step)
    assert applied.changed == 1
    assert applied.rewritten == (store.path.relative_to(comms.root),)
    assert FieldCodec.encode(store.read()) == FieldCodec.encode(expected)
    assert step.carry(apply=False).changed == step.carry(apply=True).changed == 0
    assert Bus.digest(Bus.path_in(comms.root)) == before


def test_carry_keeps_advanced_destination_and_birth_key_across_rename_and_recreation(
    carry_bus: Comms, tmp_path: Path,
) -> None:
    comms = carry_bus
    colleague = comms.registry.require("carry-colleague").incarnation.require_recorded()
    published, = TeamUpdate.published(comms, colleague)
    renamed = colleague.renamed("renamed-carry-colleague")
    advanced = replace(published, teammate=renamed,
                       not_applied=("newer retained observation",))
    TeamUpdateStore.record(comms.root, advanced)
    existing = TeamUpdateStore.for_team(comms.root, colleague)
    assert existing.path == TeamUpdateStore.for_team(comms.root, renamed).path
    recreated = RecordedThreadIncarnation(colleague.name, colleague.created_at + 1)
    recreated_packet = TeamUpdateNotice(recreated, ())
    TeamUpdateStore.record(comms.root, recreated_packet)
    distinct = TeamUpdateStore.for_team(comms.root, recreated)
    assert distinct.path != existing.path
    step = TeamObservations(LiveFilesRequest(comms.root), tmp_path / "spill")
    assert step.carry(apply=True).changed == 0
    assert FieldCodec.encode(existing.read()) == FieldCodec.encode((advanced,))
    assert distinct.read() == (recreated_packet,)


def test_packet_format_carries_precede_observation_decode() -> None:
    chain = CarryChain.at(Path(__file__).resolve().parents[1])
    position = {step: number for number, step in enumerate(chain.steps)}
    assert position[TeamUpdateLines] < position[TeamObservations]
    for predecessor in RulePingTypes.members_with(RulePingTypes):
        assert position[predecessor] < position[TeamUpdateLines]
