"""Real goal owners and ACP consumers; authored UNRUN for root qualification."""

import asyncio
from dataclasses import dataclass, field, replace
import sqlite3

from acp.schema import SessionInfoUpdate
from pydantic import BaseModel
import pytest

from agent_comms.acp_extension import GoalChangedUpdate, decode_updates
from agent_comms.child_process import ProcessIdentity
from agent_comms.comms import wire
from agent_comms.controller import SessionUpdateSink
from agent_comms.external_transcript import ExternalTranscript
from agent_comms.runtime_updates import ThreadOutput
from agent_comms.field_codec import TurnId
from agent_comms.goal_actions import EditGoalAction, SetGoalAction, StandbyGoalAction
from agent_comms.goal_attempts import Generation, GoalAttemptStore, StorageUncertainError
from agent_comms.goal_presentation import StandbyExecution
from agent_comms.goal_waits import GoalWaits
from agent_comms.locked_store import GoalWakeFile, RegistryFile
from agent_comms.repo import Checkout
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
    await agent.sessions.bind_owned(thread, thread.name)
    comms.goals.update_goal(thread.name, SetGoalAction(text="Keep this objective"))
    try:
        yield comms, agent, thread.name, client
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
    await scheduler.sync_goal_execution(session, session)
    first = client.published[-1][1]
    assert isinstance(first.execution, StandbyExecution)
    assert first.execution.inactive_wait_for == ()
    before = {path: path.read_bytes() for path in projection_files(comms)}
    output.enter(IdlePhase())
    assert {path: path.read_bytes() for path in before} == before
    await scheduler.sync_goal_execution(session, session)
    second = client.published[-1][1]
    assert second.goal == first.goal
    assert isinstance(second.execution, StandbyExecution)
    assert tuple(target.incarnation.name for target in second.execution.inactive_wait_for) == (child.name,)
    assert second.execution.presentation(session).summary == "Standby · waiting for @child; no active turn: @child"
    assert len(client.published) == 2
    reads = comms.registry.store.cache.read_counts().requests
    await scheduler.sync_goal_execution(session, session)
    assert len(client.published) == 2
    assert comms.registry.store.cache.read_counts().requests > reads


async def test_registry_routing_uses_only_the_shared_document_and_published_goal(publication):
    comms, agent, session, client = publication
    scheduler = agent.turns.goals
    await scheduler.sync_goal_execution(session, session)
    document = comms.registry.store.read()
    original = scheduler.publication(session).snapshot
    reads = comms.registry.store.cache.read_counts().requests
    await scheduler.observe_root(document, (session,), frozenset((RegistryFile.path_in(comms.root),)))
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
    await scheduler.sync_goal_execution(session, session)
    store = comms.goals.open_goal_store()
    document = comms.registry.store.read()
    before = scheduler.publication(session).snapshot
    reads = comms.registry.store.cache.read_counts().requests
    await scheduler.observe_root(document, (session,), frozenset(store.source_paths(store.path.parent)))
    assert comms.registry.store.cache.read_counts().requests > reads
    assert scheduler.publication(session).snapshot is before
    assert len(client.published) == 1


async def test_failed_goal_read_keeps_cause_and_never_creates_input_diagnostic(publication, caplog):
    comms, agent, session, client = publication
    scheduler = agent.turns.goals
    await scheduler.sync_goal_execution(session, session)
    before = scheduler.publication(session).snapshot
    diagnostics = tuple((comms.root / "diagnostics").glob("drain-*.json"))
    store = comms.goals.open_goal_store()
    path = store.path
    document = comms.registry.store.read()
    with sqlite3.connect(path) as db:
        db.execute(f'DROP TABLE "{Generation.declared_name}"')
    with pytest.raises(StorageUncertainError):
        await scheduler.sync_goal_execution(session, session)
    await scheduler.observe_root(document, (session,), frozenset(store.source_paths(path.parent)))
    assert any(record.exc_info and isinstance(record.exc_info[1], StorageUncertainError)
               for record in caplog.records)
    assert scheduler.publication(session).snapshot is before
    assert len(client.published) == 1
    assert tuple((comms.root / "diagnostics").glob("drain-*.json")) == diagnostics
