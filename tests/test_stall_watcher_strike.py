import importlib

from agent_comms.change_report import FileLine
from agent_comms.goal_actions import CreateProblemGoalAction
from agent_comms.goals import CountCheckResult, OrderCheck
from agent_comms.recorded_time import RecordedEventTime
from agent_comms.thread_identity import StallWatcherIncarnation
from agent_comms.todos import MechanicalOrderRun, PendingProblemOrder, StallReleaseTodo, StallStrikeTodo
from agent_comms.tools import CommsTodoCreateTool


def test_stall_pass_strikes_releases_and_reassigns_on_the_watcher_identity(commander, commander_script, monkeypatch):
    comms, caller = commander
    from agent_comms.commander import wheel_spin
    wheel_spin = importlib.reload(wheel_spin)
    comms.registry.declare(comms.registry.new_thread("spare", frozenset({'comms'}), str(comms.root), created_at=3.0))
    order = PendingProblemOrder(id="order-P1", owner=FileLine(__file__, 1), run=MechanicalOrderRun(),
        check=OrderCheck(str(comms.root), "printf 1", CountCheckResult(1)), edits=("Repair the owner",),
        banned=("No second store",), report="Report the result",
        at=RecordedEventTime(100), first_call_deadline=RecordedEventTime(220))
    CommsTodoCreateTool(owner="worker", order=CreateProblemGoalAction(problem="P1", order=order,
        request="request", deadline=RecordedEventTime(1000), commander="lead",
        repo="OpenHCSDev/agent-comms", text="Fix P1")).apply(comms, caller)
    # the one stubbed read: turn-digest activity marks worker and spare as recently active
    monkeypatch.setattr(wheel_spin.WheelSpin, "idle", classmethod(
        lambda cls, c, cmd: [c.registry.require("worker"), c.registry.require("spare")]))
    wheel_spin.UnownedWheelSpin.after_stall(comms)
    todo = comms.goals.todos.get("P1")
    # read back from the store: the strike keeps its kind and the watcher as its actor
    assert [type(s) for s in todo.strikes] == [StallStrikeTodo]
    assert [type(r) for r in todo.released] == [StallReleaseTodo]
    assert todo.strikes[0].actor == todo.released[0].actor == StallWatcherIncarnation.watcher()
    assert StallWatcherIncarnation.watcher().name == "stall_watcher"
    # the same pass gives the order to the idle unit not struck on it, never back to worker
    assert todo.holder == "spare" and todo.live_strikes("worker")
