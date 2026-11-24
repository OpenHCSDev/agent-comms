"""Count updates and roster updates retain their independent source cuts."""

from dataclasses import replace
from time import perf_counter
from statistics import median

from agent_comms.planning.goal_actions import PausedGoalAction, SetGoalAction
from agent_comms.presentation import ThreadView
from agent_comms.projects.checkouts import Checkout
from agent_comms.registry.threads import NativeThread
from agent_comms.registry.activity import ActivityState
from agent_comms.registry.thread_status import StoppedThreadStatus
from agent_comms.ui_model.observation import (
    ObserveSidebar,
    SidebarObserved,
)
from agent_comms.ui_model.observation_service import SidebarReadStage


def test_bus_updates_keep_roster_sources(comms, monkeypatch):
    worktree = Checkout(comms.root.parent)
    for index in range(12):
        name = f"worker-{index}"
        comms.registry.declare(NativeThread(name, frozenset(), worktree))
        comms.goals.update_goal(name, SetGoalAction(text=f"Work item {index}"))
    request = ObserveSidebar(worktree, True, True)
    stage = SidebarReadStage()
    acquired = []
    roster = ThreadView.roster

    def capture(*args, **kwargs):
        rows = roster(*args, **kwargs)
        acquired.append(rows)
        return rows

    monkeypatch.setattr(ThreadView, "roster", capture)
    first = stage.read(request, comms, comms.views.revision())
    assert isinstance(first, SidebarObserved)
    # The first real read can initialize previously absent derived-store files.
    # Settle their source revisions before measuring an already acquired reader.
    first = stage.read(request, comms, comms.views.revision())
    assert isinstance(first, SidebarObserved)
    initial_acquisitions = len(acquired)
    elapsed = []
    for index in range(6):
        sent = comms.messaging.send("worker-0", "#all", f"Count update {index}").message
        revision = comms.views.revision()
        # Hold the clock within this source epoch; expiry itself is checked below.
        revision = replace(revision, expiry_tick=first.revision.expiry_tick)
        started = perf_counter()
        answer = stage.read(request, comms, revision)
        elapsed.append(perf_counter() - started)
        assert isinstance(answer, SidebarObserved)
        assert answer.snapshot.channels["#all"].last_activity >= sent.timestamp
        assert answer.snapshot.channel_unread["#all"].count == index + 1
        assert answer.snapshot.threads == first.snapshot.threads
    print({"count_update_seconds": elapsed, "new_roster_acquisitions": len(acquired) - initial_acquisitions})
    assert len(acquired) == initial_acquisitions

    comms.goals.update_goal("worker-1", PausedGoalAction())
    paused = stage.read(request, comms, comms.views.revision())
    assert isinstance(paused, SidebarObserved)
    assert paused.snapshot.threads["worker-1"].goal_execution == comms.goals.goal_execution("worker-1")
    assert paused.snapshot.threads["worker-1"].goal_execution != first.snapshot.threads["worker-1"].goal_execution
    assert len(acquired) == initial_acquisitions + 1

    comms.registry.rename("worker-1", "renamed")
    renamed = stage.read(request, comms, comms.views.revision())
    assert isinstance(renamed, SidebarObserved)
    assert "worker-1" not in renamed.snapshot.threads
    assert renamed.snapshot.threads["renamed"].thread.incarnation == paused.snapshot.threads["worker-1"].thread.incarnation
    assert len(acquired) == initial_acquisitions + 2

    comms.agents.set_activity("worker-2", ActivityState.WORKING, "fresh activity")
    active = stage.read(request, comms, comms.views.revision())
    assert isinstance(active, SidebarObserved)
    assert active.snapshot.threads["worker-2"].activity == comms.agents.activity_of(
        "worker-2", snapshot=comms.registry.snapshot())
    assert len(acquired) == initial_acquisitions + 3

    before_birth = active.snapshot.threads["renamed"].thread.incarnation
    comms.registry.register(comms.registry.require("renamed"), status=StoppedThreadStatus())
    comms.threads.delete("renamed")
    comms.registry.declare(NativeThread("renamed", frozenset(), worktree))
    recreated = stage.read(request, comms, comms.views.revision())
    assert isinstance(recreated, SidebarObserved)
    assert recreated.snapshot.threads["renamed"].thread.incarnation != before_birth
    assert len(acquired) == initial_acquisitions + 4

    expired = stage.read(request, comms, replace(recreated.revision, expiry_tick=recreated.revision.expiry_tick + 30))
    assert isinstance(expired, SidebarObserved)
    assert len(acquired) == initial_acquisitions + 5


def test_acquired_and_fresh_roster_phases_agree(comms):
    worktree = Checkout(comms.root.parent)
    for index in range(24):
        name = f"worker-{index}"
        comms.registry.declare(NativeThread(name, frozenset(), worktree))
        comms.goals.update_goal(name, SetGoalAction(text=f"Work item {index}"))
    cut = comms.views.viewer_cut(worktree)
    revision = comms.views.revision()
    stage = SidebarReadStage()
    reads = (lambda: cut.snapshot(comms.views), lambda: stage.snapshot(cut, comms, revision))
    for read in reads:
        read()
    elapsed = ([], [])
    for index in range(20):
        answers = {}
        for which in ((0, 1) if index % 2 else (1, 0)):
            started = perf_counter()
            answers[which] = reads[which]()
            elapsed[which].append(perf_counter() - started)
        assert answers[0] == answers[1]
    print({"roster_and_assembly_median_seconds": dict(zip(("fresh", "acquired"), map(median, elapsed))),
           "iterations": 20, "threads_with_goals": 24})
