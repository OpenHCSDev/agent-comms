"""A board prepares off-loop, publishes once, and follows store revisions without beats."""

import asyncio
import os
import threading
from contextlib import nullcontext
from dataclasses import replace
from multiprocessing import Pipe
from pathlib import Path

import pytest

from agent_comms.cli_bridges.codex_connection import CodexConnection
from agent_comms.coordination.service_passes import ServicePassStore
from agent_comms.execution.child_process import (
    NoProcess,
    ProcessIdentity,
)
from agent_comms.messaging.revision_views import TeamBoardView
from agent_comms.planning.orders import (
    ProgressRead,
    TodoStore,
)
from agent_comms.presentation import WireRevision
from agent_comms.task_attachments.team_pings import TeamUpdateStore
from agent_comms.team.standing_rules import StandingRule
from agent_comms.team.team_frame_file import TeamPlanFile
from agent_comms.team.team_heartbeat import TeamBoardRead, TeamHeartbeat, TeamObservation
from agent_comms.team.team_hud import CodexUsage, TeamFrame, TeamPlan, UnreadableUsage
from agent_comms.ui_model.observation import (
    ObservationService,
    ObserveTeamHud,
    ObserveThreads,
    StopTeamHud,
    TeamHudObserved,
    TeamHudUnreadable,
    ThreadsObserved,
)
from test_holder_standing_rules import held_bus  # noqa: F401


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
    plan, huds = TeamBoardRead().read_board(held_bus)
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
    original = TeamBoardRead.read_board
    builds = []

    def blocked_read(board, comms):
        builds.append(threading.get_ident())
        loop.call_soon_threadsafe(started.set)
        assert release.wait(10), "The observer loop could not release its worker"
        return original(board, comms)

    monkeypatch.setattr(TeamBoardRead, "read_board", blocked_read)
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
            # The prepared board is the observer's view: the heartbeat's retained frames have
            # one writer, and no heartbeat ran here.
            assert TeamFrame.read_all(held_bus.root) == ()
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

    monkeypatch.setattr(TeamBoardRead, "read_board", blocked_read)
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


def test_a_pass_that_throws_leaves_the_boards_it_could_not_replace(held_bus, account_read, monkeypatch):
    """A pass that raised holds a fragment, not the team, so it may not replace the frames.

    TeamFrame.replace_all unlinks every frame absent from the huds it is handed, which is
    exactly what a teammate that stopped beating deserves and the opposite of what a pass
    that threw deserves. On 2026-11-08 one Textual worktree could not read a blob for
    patch-id, so the first reconcile raised while huds was still empty, the finally retained
    that empty fragment, and every board on disk was deleted. Tristan's reads answered
    "Tristan frame missing" for eight hours, with the alarm about the failing pass having
    nowhere left to appear.
    """
    colleague = held_bus.registry.require("colleague")
    held_bus.registry.register(
        replace(colleague, process_identity=ProcessIdentity.capture(os.getpid())))
    asyncio.run(TeamHeartbeat().run(lambda: nullcontext(held_bus)))

    def on_disk() -> dict[Path, bytes]:
        """The files themselves, because the claim is that the pass may not replace them."""
        written = {path: path.read_bytes() for path in TeamFrame.matching_in(held_bus.root)}
        plan = TeamPlanFile.path_in(held_bus.root)
        return {**written, plan: plan.read_bytes()} if plan.exists() else written

    retained = on_disk()
    assert TeamFrame.matching_in(held_bus.root), "the fixture never gave Tristan a board to lose"

    def unreadable(self, comms):
        raise RuntimeError(
            "Git in /home/ts/wt/codex-textual-keys: could not fetch blob from promisor remote")

    monkeypatch.setattr(TeamObservation, "merges", unreadable)
    # The failure stays loud: the pass raises rather than swallowing, which is what records
    # it as a failing service pass. What it may not do is take the boards down with it.
    with pytest.raises(RuntimeError, match="promisor remote"):
        asyncio.run(TeamHeartbeat().run(lambda: nullcontext(held_bus)))

    # What the pass may not do is unlink a board or replace the plan. A board it finished
    # before the merge check threw is complete (pass_body builds every board before
    # reconcile reads git) and is kept, newer, by retention.keep; so frames are compared by
    # presence and the plan, which keep never writes, by its bytes.
    after = on_disk()
    plan = TeamPlanFile.path_in(held_bus.root)
    assert after.keys() == retained.keys(), (
        "a failing merge check unlinked boards Tristan's reads still need"
    )
    assert after.get(plan) == retained.get(plan), (
        "a failing merge check replaced Tristan's plan with its own empty fragment"
    )

    # The other half, so the fix cannot degrade into never retaining at all: a pass that
    # gets through still replaces the frames. Checked by removing them and requiring the
    # next finished pass to put them back, which says nothing about who was beating. That a
    # finished pass also unlinks the frame of a teammate that stopped is replace_all's own
    # declared job (team_hud.py:374-376) and is not re-checked here.
    for path in TeamFrame.matching_in(held_bus.root):
        path.unlink()
    monkeypatch.setattr(TeamObservation, "merges", lambda self, comms: ())
    asyncio.run(TeamHeartbeat().run(lambda: nullcontext(held_bus)))
    assert TeamFrame.matching_in(held_bus.root), (
        "a finished pass wrote no frame, so this fix has degraded into never retaining"
    )
