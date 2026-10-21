"""The declared bus watch uses real native commit and replacement notifications."""

import asyncio
from contextlib import aclosing
from dataclasses import replace

import pytest

from agent_comms.bus import Bus, PublicRecord
from agent_comms.comms import Comms
from agent_comms.goal_presentation import GoalWaitTarget
from agent_comms.goal_waits import GoalWaits, RecordedGoalWait
from agent_comms.locked_store import RegistryFile
from agent_comms.messages import InfoKind, Message
from agent_comms.watched import FileSource


@pytest.fixture
def watched_bus(tmp_path):
    comms = Comms(tmp_path / "bus")
    for name in ("owner", "peer"):
        comms.registry.declare(comms.registry.new_thread(name, frozenset(), str(tmp_path)))
    return Bus.at(comms.root), comms


@pytest.fixture
def recorded_wait(watched_bus):
    _, comms = watched_bus
    return RecordedGoalWait(
        goal_id="watched-goal", wait_id="watched-wait", revision=0, after_seq=0,
        targets=(GoalWaitTarget(comms.registry.require("peer").incarnation.require_recorded()),),
        owner=comms.registry.require("owner").incarnation.require_recorded(),
        target_turn_generations=(None,), report_turn_id=None, report_turn_generation=None,
    )


def test_sqlite_wake_owners_resolve_the_commit_source_through_mro(watched_bus):
    bus, comms = watched_bus
    for owner in (Bus, GoalWaits):
        assert owner.source_paths(comms.root) == (owner.path_in(comms.root).with_suffix(".changed"),), owner.__mro__
    assert RegistryFile.source_paths(comms.root) == (RegistryFile.path_in(comms.root),)


async def test_bus_append_wakes_after_each_committed_message(watched_bus):
    bus, _ = watched_bus
    async with aclosing(bus.watch()) as observations:
        # Registration is complete before the unconditional first observation.
        await asyncio.wait_for(anext(observations), 1)
        for body in ("first", "second"):
            stored = await asyncio.to_thread(
                bus.append,
                lambda seq, db: PublicRecord.from_message(Message("peer", "owner", body, InfoKind, seq=seq)),
            )
            await asyncio.wait_for(anext(observations), 1)
            assert bus.lookup(stored.seq).body == body


async def test_goal_wait_record_wakes_after_each_commit(watched_bus, recorded_wait):
    bus, comms = watched_bus
    waits = GoalWaits(GoalWaits.path_in(comms.root))
    async with aclosing(bus.watch()) as observations:
        await anext(observations)
        for revision in (0, 1):
            await asyncio.to_thread(waits.record, replace(recorded_wait, revision=revision))
            await asyncio.wait_for(anext(observations), 1)
            assert waits.read()[recorded_wait.goal_id].revision == revision


async def test_goal_wait_read_and_bus_database_alone_do_not_wake(watched_bus, recorded_wait):
    bus, comms = watched_bus
    waits = GoalWaits(GoalWaits.path_in(comms.root))
    waits.record(recorded_wait)
    bus.append(lambda seq, db: PublicRecord.from_message(Message("peer", "owner", "seed", InfoKind, seq=seq)))
    async with aclosing(bus.watch()) as observations:
        await anext(observations)
        waiting = asyncio.create_task(anext(observations))
        try:
            assert await asyncio.to_thread(waits.read) == {recorded_wait.goal_id: recorded_wait}
            bus.path.touch()
            await asyncio.sleep(0.05)
            assert not waiting.done()
            await asyncio.to_thread(waits.record, replace(recorded_wait, revision=1))
            await asyncio.wait_for(waiting, 1)
        finally:
            waiting.cancel()
            await asyncio.gather(waiting, return_exceptions=True)


async def test_replacement_source_wakes_without_missing_the_initial_observation(watched_bus):
    bus, comms = watched_bus
    maintenance = comms.root / "maintenance-example.json"
    async with aclosing(bus.watch(FileSource(maintenance))) as observations:
        await anext(observations)
        temporary = maintenance.with_suffix(".prepared")
        temporary.write_text("published target")
        temporary.replace(maintenance)
        await asyncio.wait_for(anext(observations), 1)
