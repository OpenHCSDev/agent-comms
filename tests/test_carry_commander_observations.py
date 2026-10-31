"""Carry the previous indexed baseline without promoting a disposable HUD to history."""

from dataclasses import replace
from pathlib import Path
import time

import pytest

from agent_comms.bus import Bus
from agent_comms.commander.vibe_check import OverloadDegradation
from agent_comms.commander_hud import CommanderFrame, CommanderHud, CodexUsage, UnreadableUsage
from agent_comms.comms import Comms
from agent_comms.heartbeat_delivery import OnHeartbeatDelivery
from agent_comms.installer.carry import CarryChain
from agent_comms.locked_store import LiveFilesRequest
from agent_comms.nominal.field_codec import FieldCodec
from agent_comms.repo import Checkout
from agent_comms.standing_rules import ServicePassFailing
from agent_comms.task_attachments.commander_pings import (
    CommanderNotice, CommanderUpdate, CommanderUpdateStore,
)
from agent_comms.thread_identity import RecordedThreadIncarnation
from agent_comms.threads import Thread
from cutover.carry_commander_observations import CommanderObservations
from cutover.carry_commander_update_lines import CommanderUpdateLines
from cutover.carry_rule_ping_types import RulePingTypes


@pytest.fixture
def carry_bus(comms: Comms, tmp_path: Path) -> Comms:
    comms.registry.register(Thread(
        name="carry-lead", tags=frozenset(), worktree=Checkout(tmp_path),
        heartbeat_delivery=OnHeartbeatDelivery,
    ))
    commander = comms.registry.require("carry-lead").incarnation
    now = time.time()
    original = CommanderNotice(commander, ())
    original.send(comms, ("original quiet publication",))
    CommanderFrame.replace_all(comms.root, (CommanderHud(
        commander=commander, units=(), phases=(), forks=(),
        load=OverloadDegradation.from_orders((), 0, 0),
        usage=CodexUsage(UnreadableUsage(reason="carry fixture needs no account")),
        packets=(original,), open_pings=(),
    ),))
    failure = ServicePassFailing(watch="carry-source", since=now - 120,
                                failed_at=now - 60, error="later failure")
    latest, = CommanderUpdate.report_failure(
        commander, failure, (original,), published=(original,), now=now,
    )
    latest.send(comms, ("failure after the older HUD was saved",))
    return comms


def test_carry_preserves_indexed_failure_and_age_without_rewriting_history(
    carry_bus: Comms, tmp_path: Path,
) -> None:
    comms = carry_bus
    commander = comms.registry.require("carry-lead").incarnation
    expected = CommanderUpdate.published(comms, commander)
    stale, = CommanderFrame.at(comms.root, commander).read().packets
    assert expected[0].handled and not stale.handled
    store = CommanderUpdateStore.at(comms.root, commander)
    step = CommanderObservations(LiveFilesRequest(comms.root), tmp_path / "spill")
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
    commander = comms.registry.require("carry-lead").incarnation.require_recorded()
    published, = CommanderUpdate.published(comms, commander)
    renamed = commander.renamed("renamed-carry-lead")
    advanced = replace(published, commander=renamed,
                       not_applied=("newer retained observation",))
    CommanderUpdateStore.record(comms.root, advanced)
    existing = CommanderUpdateStore.at(comms.root, commander)
    assert existing.path == CommanderUpdateStore.at(comms.root, renamed).path
    recreated = RecordedThreadIncarnation(commander.name, commander.created_at + 1)
    recreated_packet = CommanderNotice(recreated, ())
    CommanderUpdateStore.record(comms.root, recreated_packet)
    distinct = CommanderUpdateStore.at(comms.root, recreated)
    assert distinct.path != existing.path
    step = CommanderObservations(LiveFilesRequest(comms.root), tmp_path / "spill")
    assert step.carry(apply=True).changed == 0
    assert FieldCodec.encode(existing.read()) == FieldCodec.encode((advanced,))
    assert distinct.read() == (recreated_packet,)


def test_packet_format_carries_precede_observation_decode() -> None:
    chain = CarryChain.at(Path(__file__).resolve().parents[1])
    position = {step: number for number, step in enumerate(chain.steps)}
    assert position[CommanderUpdateLines] < position[CommanderObservations]
    for predecessor in RulePingTypes.members_with(RulePingTypes):
        assert position[predecessor] < position[CommanderUpdateLines]
