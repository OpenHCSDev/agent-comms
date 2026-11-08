"""A board prepares off-loop, publishes once, and follows store revisions without beats."""

import asyncio
from dataclasses import replace
from multiprocessing import Pipe
import threading

import pytest

from test_holder_standing_rules import held_bus  # noqa: F401

from agent_comms.cli_bridges.codex_connection import CodexConnection
from agent_comms.presentation import WireRevision
from agent_comms.revision_views import TeamBoardView
from agent_comms.service_passes import ServicePassStore
from agent_comms.standing_rules import StandingRule
from agent_comms.task_attachments.team_pings import TeamUpdateStore
from agent_comms.team_heartbeat import TeamHeartbeat, TeamObservation
from agent_comms.team_hud import CodexUsage, TeamFrame, TeamPlan, UnreadableUsage
from agent_comms.todos import ProgressRead, TodoStore
from agent_comms.ui_model.observation import (
    ObservationService, ObserveTeamHud, ObserveThreads, StopTeamHud,
    TeamHudObserved, TeamHudUnreadable, ThreadsObserved,
)


async def receive(client, kind):
    """Decode the real observer pipe, rejecting a preparation error rather than timing out."""
    while True:
        if not await asyncio.to_thread(client.poll, 1):
            continue
        answer = client.recv()
        if isinstance(answer, TeamHudUnreadable):
            pytest.fail(str(answer.error))
        if isinstance(answer, kind):
            return answer


@pytest.fixture
def account_read(monkeypatch):
    """The account boundary keeps its real nested-loop requirement without a provider call."""
    async def usage():
        return CodexUsage(UnreadableUsage("test account"))

    monkeypatch.setattr(CodexConnection, "usage", lambda: asyncio.run(usage()))


def forbid_service_effect(*args, **kwargs):
    pytest.fail("Opening the board ran a heartbeat service effect")


def test_preparing_a_board_has_no_service_or_publication_effects(held_bus, account_read, monkeypatch):
    for owner, method in (
        (StandingRule, "enforce_all"), (TeamObservation, "merges"),
        (ProgressRead, "carry_reviews"), (TeamUpdateStore, "record"),
        (ServicePassStore, "heartbeat_passed"), (TeamFrame, "replace_all"),
    ):
        monkeypatch.setattr(owner, method, forbid_service_effect)
    before = held_bus.views.revision()
    plan, huds = TeamHeartbeat().read_board(held_bus)
    assert isinstance(plan, TeamPlan)
    assert huds
    assert TeamFrame.read_all(held_bus.root) == ()
    assert not before.changed_since(TeamBoardView(), held_bus.views.revision())


async def test_live_observer_serves_while_preparation_blocks_then_updates_without_beats(
    held_bus, account_read, monkeypatch,
):
    started = asyncio.Event()
    release = threading.Event()
    loop = asyncio.get_running_loop()
    original = TeamHeartbeat.read_board
    builds = []

    def blocked_read(board, comms):
        builds.append(threading.get_ident())
        loop.call_soon_threadsafe(started.set)
        assert release.wait(10), "The observer loop could not release its worker"
        return original(board, comms)

    monkeypatch.setattr(TeamHeartbeat, "read_board", blocked_read)
    before_passes = ServicePassStore.at(held_bus.root).read()
    client, observer = Pipe()
    with client, observer:
        service = ObservationService(held_bus, observer)
        running = asyncio.create_task(service.run())
        try:
            ObserveTeamHud().receive(service)
            first = await asyncio.wait_for(receive(client, TeamHudObserved), 5)
            assert first.huds == ()
            await asyncio.wait_for(started.wait(), 5)
            assert len(builds) == 1
            assert builds[0] != threading.get_ident()
            # A different UI request is answered while board preparation is held.
            ObserveThreads(frozenset({"worker"})).receive(service)
            await asyncio.wait_for(receive(client, ThreadsObserved), 5)
            assert TeamFrame.read_all(held_bus.root) == ()
            assert len(builds) == 1
            release.set()
            async with asyncio.timeout(60):
                ready = await receive(client, TeamHudObserved)
                while not ready.huds:
                    ready = await receive(client, TeamHudObserved)
            assert TeamFrame.read_all(held_bus.root) == ready.huds
            assert ServicePassStore.at(held_bus.root).read() == before_passes
            assert len(builds) == 1

            # A committed Todo row wakes the same interest, with no heartbeat call.
            started.clear()
            held_bus.goals.todos.add_request(
                held_bus.registry.require("colleague").incarnation, "New board request")
            await asyncio.wait_for(started.wait(), 10)
            assert len(builds) == 2
            refreshed = await asyncio.wait_for(receive(client, TeamHudObserved), 60)
            while refreshed.huds == ready.huds:
                refreshed = await asyncio.wait_for(receive(client, TeamHudObserved), 60)
            assert refreshed.huds
            assert ServicePassStore.at(held_bus.root).read() == before_passes
            StopTeamHud().receive(service)
        finally:
            release.set()
            service.retire()
            await asyncio.wait_for(running, 10)


async def test_retiring_interest_drops_preparation_without_a_late_publication(comms, monkeypatch):
    started = asyncio.Event()
    release = threading.Event()
    finished = threading.Event()
    loop = asyncio.get_running_loop()

    def blocked_read(board, wire):
        loop.call_soon_threadsafe(started.set)
        try:
            assert release.wait(10)
            return TeamPlan(), ()
        finally:
            finished.set()

    monkeypatch.setattr(TeamHeartbeat, "read_board", blocked_read)
    monkeypatch.setattr(TeamFrame, "replace_all", forbid_service_effect)
    client, observer = Pipe()
    with client, observer:
        service = ObservationService(comms, observer)
        running = asyncio.create_task(service.run())
        try:
            ObserveTeamHud().receive(service)
            await asyncio.wait_for(started.wait(), 5)
            StopTeamHud().receive(service)
            service.retire()
            await asyncio.wait_for(running, 5)
        finally:
            release.set()
            assert await asyncio.to_thread(finished.wait, 5)
            service.retire()
            if not running.done():
                await asyncio.wait_for(running, 5)


def test_todo_commit_signal_is_a_board_input_not_a_database_mtime(held_bus):
    paths = WireRevision.source_paths(held_bus.views)
    assert paths["team_board"] == TodoStore.source_paths(held_bus.root)
    assert TodoStore.path_in(held_bus.root) not in paths["team_board"]
    before = held_bus.views.revision()
    held_bus.goals.todos.add_request(
        held_bus.registry.require("colleague").incarnation, "Board input")
    assert before.changed_since(TeamBoardView(), held_bus.views.revision())


def test_frame_publication_and_expiry_do_not_request_another_preparation():
    before = WireRevision.empty()
    assert not replace(before, expiry_tick=60).changed_since(TeamBoardView(), before)
