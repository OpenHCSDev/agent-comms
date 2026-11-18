"""Real goal owners publish goal changes to their ACP consumers."""

import asyncio
import sqlite3
import sys
from dataclasses import dataclass, field, replace
from functools import partial

import pytest
from acp.schema import SessionInfoUpdate
from agent_comms.execution.native_child import PiChild
from agent_comms.execution.pi_durable_backend import PiDurableBackend
from agent_comms.transport.acp_update_sink import AcpUpdateSink
from delivery_owner_fixture import canonical_agent
from goal_owner_fixture import canonical_goal_wire
from pydantic import BaseModel

from agent_comms.acp_extension import GoalChangedUpdate, decode_updates
from agent_comms.cli_bridges.runtime_updates import ThreadOutput
from agent_comms.comms import wire
from agent_comms.execution.child_process import ProcessIdentity
from agent_comms.execution.input_drain import UnreadRegistry
from agent_comms.execution.transcript_sources import ExternalTranscript
from agent_comms.execution.turn_phase import IdlePhase, ToolRunningPhase
from agent_comms.execution.turn_lease import TurnRecord
from agent_comms.nominal.field_codec import AcpSessionId, MalformedRecord, TurnId
from agent_comms.planning.goal_actions import (
    EditGoalAction,
    SetGoalAction,
    StandbyGoalAction,
)
from agent_comms.planning.goal_attempts import GoalAttemptStore
from agent_comms.planning.goal_ledger import Generation, StorageUncertainError
from agent_comms.planning.goal_presentation import StandbyExecution
from agent_comms.planning.goal_waits import GoalWaits
from agent_comms.projects.checkouts import Checkout
from agent_comms.registry.registry_document import RegistryDocument
from agent_comms.registry.thread_text import GivenThreadText
from agent_comms.registry.threads import ExternalThread, NativeThread
from agent_comms.storage.locked_store import GoalWakeFile, RegistryFile


@dataclass
class GoalLog(AcpUpdateSink):
    published: list[tuple[str, GoalChangedUpdate]] = field(default_factory=list)

    async def publish_update(self, *, session_id: str, update: BaseModel) -> bool:
        # ACP is the foreign boundary; the production GoalChanged consumer and
        # protocol codec still do all publication work.
        if isinstance(update, SessionInfoUpdate):
            self.published.extend((session_id, item) for item in decode_updates(update.field_meta)
                                  if isinstance(item, GoalChangedUpdate))
        return True


class CountedWake(asyncio.Event):
    def __init__(self):
        super().__init__()
        self.calls = 0

    def set(self) -> None:
        self.calls += 1
        super().set()


async def until(predicate):
    async with asyncio.timeout(8):
        while not predicate():
            await asyncio.sleep(0.01)


@pytest.fixture
async def publication(tmp_path, pinned_native):
    comms = canonical_goal_wire(tmp_path / "wire")
    agent = canonical_agent(comms, auto_wake=False)
    client = GoalLog()
    agent.on_connect(client)
    thread = agent.sessions.declare_thread(str(tmp_path))
    await agent.sessions.bind_owned(thread, AcpSessionId(thread.name))
    comms.goals.update_goal(thread.name, SetGoalAction(text="Keep this objective"))
    try:
        yield comms, agent, agent.inputs.state(thread.name).owned().identifier, client
    finally:
        await agent.shutdown()


def projection_files(comms):
    return (comms.registry.store.path, GoalWaits.path_in(comms.root),
            GoalAttemptStore.path_in(GoalAttemptStore.directory_in(comms.root)))


async def test_requested_refresh_observes_activity_with_unchanged_durable_goal_files(publication):
    comms, agent, session, client = publication
    child = comms.registry.declare(ExternalThread(
        "child", frozenset(), Checkout(comms.root),
        process_identity=ProcessIdentity.current()))
    output = ThreadOutput(ExternalTranscript(comms.root, child.incarnation), comms.agents)
    output.enter(ToolRunningPhase("delegated work"))
    comms.goals.update_goal(session, StandbyGoalAction(wait_for=(child.name,)))
    scheduler = agent.turns.goals
    await scheduler.sync_goal_execution(session)
    first = client.published[-1][1]
    assert isinstance(first.execution, StandbyExecution)
    assert first.execution.inactive_wait_for == ()
    before = {path: path.read_bytes() for path in projection_files(comms)}
    output.enter(IdlePhase())
    assert {path: path.read_bytes() for path in before} == before
    await scheduler.sync_goal_execution(session)
    second = client.published[-1][1]
    assert second.goal == first.goal
    assert isinstance(second.execution, StandbyExecution)
    assert tuple(target.incarnation.name for target in second.execution.inactive_wait_for) == (child.name,)
    assert second.execution.presentation(session).summary == "Standby · waiting for @child; no active turn: @child"
    await scheduler.sync_goal_execution(session)
    assert len(client.published) == 2


async def test_registry_routing_uses_only_the_shared_document_and_published_goal(publication):
    comms, agent, session, client = publication
    scheduler = agent.turns.goals
    await scheduler.sync_goal_execution(session)
    document = comms.registry.store.read()
    original = scheduler.publication(session).snapshot
    reads = comms.registry.store.cache.read_counts().requests
    await asyncio.gather(*scheduler.observe_root(
        document, (agent.inputs.state(session).owned(),),
        frozenset((RegistryFile.path_in(comms.root),)),
    ))
    assert comms.registry.store.cache.read_counts().requests == reads
    assert scheduler.publication(session).snapshot is original
    assert len(client.published) == 1
    assert GoalWaits.source_paths(comms.root)[0] in GoalWakeFile.execution_paths_in(comms.root)
    assert RegistryFile.path_in(comms.root) not in GoalWakeFile.execution_paths_in(comms.root)


async def test_external_mid_turn_goal_reaches_actual_consumer_without_inbox_wake(publication):
    comms, agent, session, client = publication
    # The durable native turn is real; there is deliberately no provider and no
    # accepted bus input. Its busy fence prevents an autonomous model launch.
    lease = comms.agents.begin_turn(session, TurnId.mint()).turn_lease
    unrelated = comms.registry.declare(NativeThread("unrelated", frozenset(), Checkout(comms.root)))
    state = agent.inputs.state(session)
    wake = CountedWake()
    state.observation_ready = wake
    agent.inputs.ensure_live_drain(session)
    scheduler = agent.turns.goals
    try:
        await until(lambda: state.observation_watcher is not None and state.inbox.quiet
                    and not state.observation_ready.is_set()
                    and scheduler.publication(session).snapshot.goal_changed(
                        comms.registry.require(session).goal) is False)
        before_messages = comms.bus.bus.revision()
        before_wakes, before_events = wake.calls, len(client.published)
        previous_read = agent.inputs.observed_registry
        wire(comms.root).registry.register(replace(unrelated, title=GivenThreadText("unrelated edit")))
        await until(lambda: agent.inputs.observed_registry is not previous_read)
        assert wake.calls == before_wakes
        assert len(client.published) == before_events
        changed = wire(comms.root).goals.update_goal(session, EditGoalAction(text="Changed externally"))
        await until(lambda: client.published[-1][1].goal == changed)
        assert len(client.published) == before_events + 1
        assert wake.calls == before_wakes
        assert comms.bus.bus.revision() == before_messages
        assert comms.registry.require(session).turn_lease == lease
    finally:
        comms.agents.finish_turn(lease)


async def test_a_wake_rereads_the_registry_only_when_it_committed_or_the_sessions_changed():
    reads = []

    def read(names):
        reads.append(names)
        return asyncio.ensure_future(asyncio.sleep(0))

    first = UnreadRegistry().following(("a",), False, read)
    assert first.following(("a",), False, read) is first
    committed = first.following(("a",), True, read)
    joined = committed.following(("a", "b"), False, read)
    assert reads == [("a",), ("a",), ("a", "b")]
    assert joined.following(("a", "b"), False, read) is joined
    await asyncio.gather(first.read, committed.read, joined.read)


async def test_attempt_signal_refreshes_complete_projection_but_does_not_publish_an_identical_one(publication):
    comms, agent, session, client = publication
    scheduler = agent.turns.goals
    await scheduler.sync_goal_execution(session)
    store = comms.goals.open_goal_store()
    document = comms.registry.store.read()
    before = scheduler.publication(session).snapshot
    reads = comms.registry.store.cache.read_counts().requests
    await asyncio.gather(*scheduler.observe_root(
        document, (agent.inputs.state(session).owned(),),
        frozenset(store.source_paths(store.path.parent)),
    ))
    assert comms.registry.store.cache.read_counts().requests > reads
    assert scheduler.publication(session).snapshot is before
    assert len(client.published) == 1


async def test_failed_goal_read_keeps_cause_and_never_creates_input_diagnostic(publication, caplog):
    comms, agent, session, client = publication
    scheduler = agent.turns.goals
    await scheduler.sync_goal_execution(session)
    before = scheduler.publication(session).snapshot
    diagnostics = tuple((comms.root / "diagnostics").glob("drain-*.json"))
    store = comms.goals.open_goal_store()
    path = store.path
    document = comms.registry.store.read()
    with sqlite3.connect(path) as db:
        db.execute(f'DROP TABLE "{Generation.declared_name}"')
    with pytest.raises(StorageUncertainError):
        await scheduler.sync_goal_execution(session)
    outcomes = await asyncio.gather(*scheduler.observe_root(
        document, (agent.inputs.state(session).owned(),),
        frozenset(store.source_paths(path.parent)),
    ), return_exceptions=True)
    assert len(outcomes) == 1 and isinstance(outcomes[0], StorageUncertainError)
    assert any(record.exc_info and isinstance(record.exc_info[1], StorageUncertainError)
               for record in caplog.records)
    assert scheduler.publication(session).snapshot is before
    assert len(client.published) == 1
    assert tuple((comms.root / "diagnostics").glob("drain-*.json")) == diagnostics


async def test_idle_retirement_never_acquires_an_unloaded_session(publication):
    _comms, agent, _session, _client = publication
    missing = AcpSessionId("never-loaded")
    before = dict(agent.inputs.drained)
    await agent.turns.close_idle_backend(missing)
    assert agent.inputs.drained == before
    assert missing not in agent.turns.persistent_backends


async def test_idle_retirement_uses_the_backend_contract_and_respects_a_held_turn(publication):

    comms, agent, session, _client = publication
    backend = PiDurableBackend()
    child = await PiChild.start_command(
        (sys.executable, "-c", "import sys; sys.stdin.buffer.read()"), cwd=str(comms.root),
    )
    backend.child = child
    agent.turns.persistent_backends[session] = backend
    try:
        async with agent.inputs.state(session).turn_lock:
            await agent.turns.close_idle_backend(session)
            assert backend.child is child
            assert child.proc.alive()
        await agent.turns.close_idle_backend(session)
        assert backend.child is None
        assert child.retired
    finally:
        await backend.close_idle()


async def test_observation_joins_before_retirement_and_captured_sources_cannot_reopen(publication):
    comms, agent, session, client = publication
    owner = agent.inputs.state(session).owned()
    document = comms.registry.store.read()
    entered, release = (asyncio.Event(), asyncio.Event()), asyncio.Event()

    async def observation(started):
        started.set()
        await release.wait()
        assert agent.sessions.require(session) == session
        assert agent.inputs.state(session) is owner

    observing = tuple(asyncio.create_task(owner.observe_live(partial(observation, started)))
                      for started in entered)
    stopping = None
    try:
        await asyncio.wait_for(asyncio.gather(*(started.wait() for started in entered)), 3)
        fenced = comms.owners.fence_stop(session)
        request, = comms.owners.stop_requests(fenced)
        stopping = asyncio.create_task(request.retire(agent.sessions, owner))
        await asyncio.sleep(0)
        assert not stopping.done()
        release.set()
        await asyncio.wait_for(asyncio.gather(*observing, stopping), 8)
        assert owner.retired.done()
        assert session not in agent.sessions.published
        assert session not in agent.inputs.drained
        before = len(client.published)
        await owner.observe_live(lambda: agent.sessions.config.refresh_configuration(session))
        for source in (document, RegistryDocument()):
            await asyncio.gather(*agent.turns.goals.observe_root(
                source, (owner,), frozenset((RegistryFile.path_in(comms.root),)),
            ))
        assert len(client.published) == before
        assert session not in agent.inputs.drained
        assert session not in agent.turns.persistent_backends
    finally:
        release.set()
        await asyncio.gather(*(task for task in (*observing, stopping) if task is not None),
                             return_exceptions=True)


async def test_queued_initial_menu_obeys_the_original_owner_stop(publication):
    _comms, agent, session, client = publication
    owner = agent.inputs.state(session).owned()
    menu = agent.sessions.config.start_menu(session, client)
    owner.request_stop()
    await asyncio.wait_for(menu, 3)
    assert not agent.turns.persistent_backends


async def test_cancelled_observation_releases_retirement_admission(publication):
    _comms, agent, session, _client = publication
    owner = agent.inputs.state(session).owned()
    entered = asyncio.Event()

    async def observation():
        entered.set()
        await asyncio.Future()

    observing = asyncio.create_task(owner.observe_live(observation))
    try:
        await asyncio.wait_for(entered.wait(), 3)
        observing.cancel()
        with pytest.raises(asyncio.CancelledError):
            await observing
        await asyncio.wait_for(owner.join_observations(), 3)
    finally:
        observing.cancel()
        await asyncio.gather(observing, return_exceptions=True)


async def hosted_goals(comms, agent, total):
    sessions = []
    for index in range(total):
        directory = comms.root / f"hosted-{index}"
        directory.mkdir()
        thread = agent.sessions.declare_thread(str(directory))
        session = AcpSessionId(thread.name)
        await agent.sessions.bind_owned(thread, session)
        comms.goals.update_goal(thread.name, SetGoalAction(text=f"Objective {index}"))
        sessions.append(session)
    return tuple(sessions)


async def test_ready_goal_publications_share_one_source_cut_and_late_locks_read_fresh(
    publication, monkeypatch,
):
    comms, agent, first, client = publication
    others = await hosted_goals(comms, agent, 2)
    sessions = (first, *others)
    scheduler = agent.turns.goals
    leases = [comms.agents.begin_turn(session, TurnId.mint()).turn_lease for session in sessions]
    snapshots, turns, generations, waits = [], [], [], []
    original_registry = type(comms.registry.store).snapshot
    original_turns = type(comms.registry.store).capture_turns
    original_generations = GoalAttemptStore.reader_snapshot
    original_waits = GoalWaits.snapshot

    def registry(store):
        snapshots.append(store.path)
        return original_registry(store)

    def turn_source(store, held):
        turns.append(tuple(held))
        return original_turns(store, held)

    def generation_source(store):
        generations.append(store.path)
        return original_generations(store)

    def wait_source(store):
        waits.append(store.path)
        return original_waits(store)

    try:
        with monkeypatch.context() as patch:
            patch.setattr(type(comms.registry.store), "snapshot", registry)
            patch.setattr(type(comms.registry.store), "capture_turns", turn_source)
            patch.setattr(GoalAttemptStore, "reader_snapshot", generation_source)
            patch.setattr(GoalWaits, "snapshot", wait_source)
            await asyncio.gather(*(scheduler.sync_goal_execution(session) for session in sessions))
            assert len(snapshots) == len(generations) == len(waits) == 1
            # READY goals need no turn evidence; batching must keep that lazy boundary.
            assert turns == []
            snapshots.clear()
            generations.clear()
            waits.clear()
            for session in sessions:
                comms.goals.goal_changed(session, scheduler.publication(session).snapshot)
            assert len(snapshots) == len(generations) == len(waits) == len(sessions)
            assert turns == []
        assert {session for session, _update in client.published} == set(sessions)
        before = {session: scheduler.publication(session).snapshot for session in sessions}
        await asyncio.gather(*(scheduler.sync_goal_execution(session) for session in sessions))
        assert all(scheduler.publication(session).snapshot is before[session] for session in sessions)
        assert len(client.published) == len(sessions)

        # The first owner's queued read must not borrow the other owners' earlier cut.
        async with scheduler.publication(first).lock:
            delayed = asyncio.create_task(scheduler.sync_goal_execution(first))
            await scheduler.sync_goal_execution(others[0])
            changed = comms.goals.update_goal(first, EditGoalAction(text="After the earlier cut"))
            assert not delayed.done()
        await delayed
        assert scheduler.publication(first).snapshot.goal == changed
        assert client.published[-1][1].goal == changed
    finally:
        for lease in leases:
            comms.agents.finish_turn(lease)


async def test_batched_goal_read_keeps_each_source_failure_and_retries_failed_dispatch(
    publication, monkeypatch,
):
    comms, agent, first, client = publication
    second, = await hosted_goals(comms, agent, 1)
    scheduler = agent.turns.goals
    requests = (
        ("never-registered", scheduler.publication(first).snapshot),
        (first, scheduler.publication(first).snapshot),
        (second, scheduler.publication(second).snapshot),
    )
    failed, *good = comms.goals.goal_changes(requests)
    with pytest.raises(KeyError):
        await failed.publish(lambda event: asyncio.sleep(0), requests[0][1])
    for event, (_, previous) in zip(good, requests[1:], strict=True):
        assert await event.publish(lambda event: asyncio.sleep(0), previous) is not previous

    original = client.publish_update
    fail = True

    async def dispatch(*, session_id, update):
        if session_id == first and fail:
            raise OSError("Original consumer failed")
        return await original(session_id=session_id, update=update)

    monkeypatch.setattr(client, "publish_update", dispatch)
    before = scheduler.publication(first).snapshot
    outcomes = await asyncio.gather(*(scheduler.sync_goal_execution(session)
                                      for session in (first, second)), return_exceptions=True)
    assert isinstance(outcomes[0], OSError)
    assert outcomes[1] is None
    assert scheduler.publication(first).snapshot is before
    assert {session for session, _update in client.published} == {second}
    fail = False
    await scheduler.sync_goal_execution(first)
    assert scheduler.publication(first).snapshot is not before
    assert {session for session, _update in client.published} == {first, second}


async def test_cancelled_goal_reader_joins_its_batch_without_cancelling_the_peer(
    publication, monkeypatch,
):
    _comms, agent, first, client = publication
    scheduler = agent.turns.goals
    entered, release = asyncio.Event(), asyncio.Event()
    original = scheduler.read_goal_changes

    async def held_batch():
        entered.set()
        await release.wait()
        await original()

    monkeypatch.setattr(scheduler, "read_goal_changes", held_batch)
    previous = scheduler.publication(first).snapshot
    reader = asyncio.create_task(scheduler.read_goal_change(first, previous))
    peer = asyncio.create_task(scheduler.read_goal_change(first, previous))
    try:
        await entered.wait()
        reader.cancel()
        await asyncio.sleep(0)
        assert not reader.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await reader
        event = await peer
        assert await event.publish(lambda event: asyncio.sleep(0), previous) is not previous
        assert client.published == []
    finally:
        release.set()
        await asyncio.gather(reader, peer, return_exceptions=True)


async def test_captured_turn_rows_decode_independently_and_keep_their_original_cut(publication):
    comms, agent, first, _client = publication
    second, = await hosted_goals(comms, agent, 1)
    leases = tuple(comms.agents.begin_turn(session, TurnId.mint()).turn_lease
                   for session in (first, second))
    good, bad = (lease.record for lease in leases)
    rows = comms.registry.store.rows
    original = rows.turn_wires((bad.identity,))[bad.identity]
    try:
        # Deliberately corrupt one foreign stored wire, through its real row owner.
        rows.change_turn_wire(bad.identity, lambda _wire: (({"not_a_turn": True}, None),))
        captured = TurnRecord.read_all((good, bad))
        actual = captured[good]
        assert actual == good.read()
        assert captured[good] is actual
        with pytest.raises(MalformedRecord):
            captured[bad]
        rows.change_turn_wire(bad.identity, lambda _wire: ((original, None),))
        assert TurnRecord.read_all((bad,))[bad] == bad.read()
        # Repairing the live row must not rewrite the cut that supplied the old failure.
        with pytest.raises(MalformedRecord):
            captured[bad]
    finally:
        rows.change_turn_wire(bad.identity, lambda _wire: ((original, None),))
        for lease in leases:
            comms.agents.finish_turn(lease)
