"""Real goal owners and ACP consumers; authored UNRUN for root qualification."""

import asyncio
from dataclasses import dataclass, field, replace
from functools import partial
import sqlite3
import sys

from acp.schema import SessionInfoUpdate
from pydantic import BaseModel
import pytest

from agent_comms.acp_extension import GoalChangedUpdate, decode_updates
from agent_comms.child_process import ProcessIdentity
from agent_comms.comms import wire
from agent_comms.controller import SessionUpdateSink
from agent_comms.external_transcript import ExternalTranscript
from agent_comms.runtime_updates import ThreadOutput
from agent_comms.nominal.field_codec import AcpSessionId, TurnId
from agent_comms.goal_actions import EditGoalAction, SetGoalAction, StandbyGoalAction
from agent_comms.goal_attempts import Generation, GoalAttemptStore, StorageUncertainError
from agent_comms.goal_presentation import StandbyExecution
from agent_comms.goal_waits import GoalWaits
from agent_comms.locked_store import GoalWakeFile, RegistryFile
from agent_comms.repo import Checkout
from agent_comms.registry_document import RegistryDocument
from agent_comms.thread_execution import ExternalThreadExecution
from agent_comms.thread_text import GivenThreadText
from agent_comms.threads import Thread
from agent_comms.turn_phase import IdlePhase, ToolRunningPhase
from delivery_owner_fixture import canonical_agent
from goal_owner_fixture import canonical_goal_wire


@dataclass
class GoalLog(SessionUpdateSink):
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
async def publication(tmp_path):
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
    child = comms.registry.declare(Thread(
        "child", frozenset(), Checkout(comms.root), execution=ExternalThreadExecution,
        process_identity=ProcessIdentity.current(),
    ))
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
    assert len(client.published) == 2
    reads = comms.registry.store.cache.read_counts().requests
    await scheduler.sync_goal_execution(session)
    assert len(client.published) == 2
    assert comms.registry.store.cache.read_counts().requests > reads


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
    unrelated = comms.registry.declare(Thread("unrelated", frozenset(), Checkout(comms.root)))
    state = agent.inputs.state(session)
    wake = CountedWake()
    state.observation_ready = wake
    agent.inputs.ensure_live_drain(session)
    scheduler = agent.turns.goals
    try:
        await until(lambda: state.observation_watcher is not None and state.settled()
                    and not state.observation_ready.is_set() and not state.repeat_observation
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
    from agent_comms.native_child import PiChild
    from agent_comms.pi_durable_backend import PiDurableBackend

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
