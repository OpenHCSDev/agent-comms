import asyncio

from agent_comms.commander import standing
from agent_comms.change_report import FileLine
from agent_comms.goal_actions import CreateProblemGoalAction
from agent_comms.goals import CountCheckResult, OrderCheck
from agent_comms.recorded_time import RecordedEventTime
from agent_comms.thread_identity import StallWatcherIncarnation
from agent_comms.todos import MechanicalOrderRun, PendingProblemOrder, ReleaseTodo, StrikeTodo
from agent_comms.tools import CommsTodoCreateTool


def test_stall_pass_strikes_releases_and_reassigns_on_the_watcher_identity(commander, commander_script, monkeypatch):
    comms, caller = commander
    comms.registry.declare(comms.registry.new_thread("spare", frozenset({'comms'}), str(comms.root), created_at=3.0))
    order = PendingProblemOrder(id="order-P1", owner=FileLine(__file__, 1), run=MechanicalOrderRun(),
        check=OrderCheck(str(comms.root), "printf 1", CountCheckResult(1)), edits=("Repair the owner",),
        banned=("No second store",), report="Report the result",
        at=RecordedEventTime(100), first_call_deadline=RecordedEventTime(220))
    CommsTodoCreateTool(owner="worker", order=CreateProblemGoalAction(problem="P1", order=order,
        request="request", deadline=RecordedEventTime(1000), commander="lead",
        repo="OpenHCSDev/agent-comms", text="Fix P1")).apply(comms, caller)
    # the one stubbed read: turn-digest activity marks worker and spare as recently active
    monkeypatch.setattr(standing.WheelSpin, "idle", classmethod(
        lambda cls, c, cmd: [c.registry.require("worker"), c.registry.require("spare")]))
    standing.UnownedWheelSpin.after_stall(comms)
    todo = comms.goals.todos.get("P1")
    # read back from the store: the strike keeps its kind and the watcher as its actor
    assert [type(s) for s in todo.strikes] == [StrikeTodo]
    assert [type(r) for r in todo.released] == [ReleaseTodo]
    assert todo.strikes[0].actor == todo.released[0].actor == StallWatcherIncarnation.watcher()
    assert StallWatcherIncarnation.watcher().name == "stall_watcher"
    # the same pass gives the order to the idle unit not struck on it, never back to worker
    assert todo.holder == "spare" and todo.live_strikes("worker")
    # Core's watcher made the assignment, while the order stays on its commander's board
    assert todo.assignment.parent_identity == StallWatcherIncarnation.watcher()
    assert [t.id for t in comms.goals.todos.held_for_commander(comms.registry.require("lead").incarnation)] == ["P1"]


def test_a_failed_stall_pass_is_recorded_and_the_next_pass_still_runs(commander, monkeypatch):
    comms, _ = commander
    passes = []

    def after_stall(cls, c):
        # the second pass runs after the first raised, and sees the first's recorded failure
        passes.append([f.error for f in c.goals.todos.stall_pass_failures()])
        if len(passes) == 1:
            raise FileNotFoundError("no commander config")
        c.goals.todos.stall_pass_succeeded()

    async def wait(seconds):  # the watcher stops after its second pass
        if len(passes) == 2:
            raise asyncio.CancelledError

    monkeypatch.setattr(standing.UnownedWheelSpin, "after_stall", classmethod(after_stall))
    monkeypatch.setattr(standing.asyncio, "sleep", wait)
    try:
        asyncio.run(standing.UnownedWheelSpin.watch(comms))
    except asyncio.CancelledError:
        pass
    assert passes == [[], ["FileNotFoundError: no commander config"]]
    assert comms.goals.todos.stall_pass_failures() == ()


def test_parked_threads_are_core_facts_stored_with_the_todos(commander):
    comms, _ = commander
    comms.goals.todos.park("worker")
    assert comms.goals.todos.parked() == frozenset({"worker"})
    comms.goals.todos.unpark("worker")
    assert comms.goals.todos.parked() == frozenset()
