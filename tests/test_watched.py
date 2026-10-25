"""The declared bus watch uses real native commit and replacement notifications."""


import asyncio
import errno
from contextlib import aclosing
from concurrent.futures import Future
from dataclasses import replace
from pathlib import Path

import pytest

from agent_comms.field_codec import GoalId
from agent_comms.bus import Bus, PublicRecord
from agent_comms.comms import wire
from agent_comms.goal_attempts import Generation, GoalAttemptStore, StorageUncertainError
from agent_comms.errors import UnresolvedAttemptError
from agent_comms.goal_presentation import GoalWaitTarget
from agent_comms.goal_waits import GoalWaits, RecordedGoalWait
from agent_comms.locked_store import RegistryFile
from agent_comms.messages import InfoKind, Message
from agent_comms.watched import ChangeSignal, FileSource
from agent_comms.private_path import FileRevision
from agent_comms.view_unread import (
    NativeReplyPreparation, ReplyIndex, ReplyIndexUpdate, ReplyRead, ReplyReading,
    TranscriptReadState,
)
from agent_comms.threads import Thread


@pytest.fixture
def watched_bus(tmp_path):
    Bus.install(Bus.path_in(tmp_path / "bus"))
    comms = wire(tmp_path / "bus")
    for name in ("owner", "peer"):
        comms.registry.declare(Thread(name, frozenset(), str(tmp_path)))
    return Bus.at(comms.root), comms


@pytest.fixture
def recorded_wait(watched_bus):
    watched_goal_id = GoalId.mint()
    _, comms = watched_bus
    return RecordedGoalWait(
        goal_id=watched_goal_id, wait_id="watched-wait", revision=0, after_seq=0,
        targets=(GoalWaitTarget(comms.registry.require("peer").incarnation.require_recorded()),),
        owner=comms.registry.require("owner").incarnation.require_recorded(),
        target_turn_generations=(None,), report_turn_id=None, report_turn_generation=None,
    )


def test_sqlite_wake_owners_resolve_the_commit_source_through_mro(watched_bus):
    bus, comms = watched_bus
    for owner in (Bus, GoalWaits):
        marker, = owner.source_paths(comms.root)
        assert marker.parent != owner.path_in(comms.root).parent
        assert marker in owner.transient_paths(owner.path_in(comms.root))
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


@pytest.fixture
def private_ledger(watched_bus):
    _, comms = watched_bus
    root = comms.root / GoalAttemptStore.root_directory
    root.mkdir(mode=0o700)
    return GoalAttemptStore.initialize(root)


async def test_private_goal_commits_wake_but_reads_do_not(watched_bus, private_ledger):
    first_goal_id = GoalId.mint()
    second_goal_id = GoalId.mint()
    bus, _ = watched_bus
    async with aclosing(bus.watch()) as observations:
        await anext(observations)
        for goal in (first_goal_id, second_goal_id):
            await asyncio.to_thread(private_ledger.create_goal, goal)
            await asyncio.wait_for(anext(observations), 1)
        waiting = asyncio.create_task(anext(observations))
        try:
            assert private_ledger.snapshot(first_goal_id).goal_id == first_goal_id
            await asyncio.sleep(0.05)
            assert not waiting.done()
        finally:
            waiting.cancel()
            await asyncio.gather(waiting, return_exceptions=True)


def test_private_failed_readback_does_not_publish(private_ledger):
    retained_goal_id = GoalId.mint()
    private_ledger.create_goal(retained_goal_id)
    with Bus.register_sources(*(FileSource(path) for path in
                               private_ledger.source_paths(private_ledger.root))) as changes:
        with pytest.raises(StorageUncertainError):
            private_ledger._change(
                lambda db: Generation.delete(db, goal_id=retained_goal_id), lambda _: False,
            )
        assert private_ledger.snapshot(retained_goal_id) is None
        assert changes.changed_paths() == frozenset()


def test_private_notification_failure_returns_no_ready_grant(private_ledger):
    uncertain_goal_id = GoalId.mint()
    marker, = private_ledger.source_paths(private_ledger.root)
    marker.mkdir()
    with pytest.raises(StorageUncertainError):
        private_ledger.create_goal(uncertain_goal_id)
    assert private_ledger.snapshot(uncertain_goal_id).goal_id == uncertain_goal_id
    with pytest.raises(UnresolvedAttemptError):
        private_ledger.ready_grant(uncertain_goal_id, 1)


def move_away(path: Path) -> None:
    path.rename(path.with_name("removed"))


@pytest.mark.parametrize("remove", (Path.unlink, move_away))
async def test_native_file_removal_wakes_without_unrelated_events(tmp_path, remove):
    auth = tmp_path / "auth.json"
    auth.write_text("{}")
    async with ChangeSignal.opened(FileSource(auth)) as changes:
        changes.prepare()
        unrelated = tmp_path / "unrelated"
        unrelated.write_text("not observed")
        unrelated.unlink()
        with pytest.raises(TimeoutError):
            async with asyncio.timeout(0.05):
                await changes.wait()
        remove(auth)
        await asyncio.wait_for(changes.wait(), 1)


async def test_failed_native_descriptor_reaches_waiting_reader_and_survives_later_wake(tmp_path):
    path = tmp_path / "observed"
    async with ChangeSignal.opened(FileSource(path)) as changes:
        changes.prepare()
        # Retire the real native descriptor before a queued readiness callback runs.
        # ExitStack close is idempotent; context retirement will not close it twice.
        changes.close()
        changes._on_ready()
        changes.wake()
        with pytest.raises(OSError) as failed:
            await asyncio.wait_for(changes.wait(), 1)
        assert failed.value.errno == errno.EBADF


async def test_native_file_burst_wakes_before_the_producer_finishes(tmp_path):
    path = tmp_path / "stream"
    path.write_text("0")

    async def produce():
        for revision in range(50):
            path.write_text(str(revision))
            await asyncio.sleep(0.005)

    async with ChangeSignal.opened(FileSource(path)) as changes:
        changes.prepare()
        producer = asyncio.create_task(produce())
        try:
            await asyncio.wait_for(changes.wait(), 0.15)
            assert not producer.done()
        finally:
            producer.cancel()
            await asyncio.gather(producer, return_exceptions=True)


async def test_native_source_survives_directory_removal_and_recreation(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    path = root / "published"
    async with aclosing(Bus.watch_sources(FileSource(path))) as observations:
        await anext(observations)
        root.rename(tmp_path / "old")
        await asyncio.wait_for(anext(observations), 1)
        root.mkdir()
        await asyncio.wait_for(anext(observations), 1)
        path.write_text("new root")
        await asyncio.wait_for(anext(observations), 1)


def test_native_reply_publication_uses_the_index_commit_source(tmp_path):
    transcript = tmp_path / "session.jsonl"
    transcript.write_text("")
    state = TranscriptReadState(tmp_path / "read.json")
    try:
        database = state._database()
        before = ReplyIndex(source=str(transcript))
        revision = FileRevision.capture(transcript).require(ValueError("Missing transcript"))
        read = ReplyRead(before, revision, 0)
        prepared = Future()
        prepared.set_result(ReplyIndexUpdate(before, replace(before, revision=revision), ()))
        preparation = NativeReplyPreparation({str(transcript): ReplyReading(read, prepared)})
        with Bus.register_sources(*(FileSource(path) for path in
                                   state.source_paths(tmp_path))) as changes:
            assert preparation.finished(state, database, (str(transcript),)) is state._idle
            assert changes.changed_paths() == frozenset(state.source_paths(tmp_path))
            # Reader attachment and counts are not committed work.
            assert ReplyIndex.one(database, source=str(transcript)).revision == revision
            assert changes.changed_paths() == frozenset()
    finally:
        state.close()


def test_retired_native_events_invalidate_registered_source_coverage(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    path = root / "observed"
    unrelated = tmp_path / "another-source"
    with Bus.register_sources(FileSource(path), FileSource(unrelated)) as changes:
        root.rename(tmp_path / "retired")
        assert path in changes.changed_paths()
        # Removing the old native registration queues its final events after
        # the rename batch. Their retired descriptor cannot prove quietness.
        assert changes.changed_paths() == frozenset((path, unrelated))


def test_synchronous_backlog_invalidates_admitted_sources(tmp_path):
    path = tmp_path / "observed"
    with Bus.register_sources(FileSource(path)) as changes:
        # More than a single native read, without changing the admitted file.
        for index in range(2048):
            (tmp_path / str(index)).write_text("unrelated")
        assert changes.changed_paths() == frozenset((path,))
