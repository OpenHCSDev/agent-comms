"""Background runs: one family for shell commands and deferred tool calls, settled by one
watcher, told once, and recovered after the bridge that started them is gone."""

from __future__ import annotations

import os
import subprocess
import time
from collections.abc import Callable
from contextlib import suppress
from dataclasses import replace
from pathlib import Path

import pytest

from agent_comms.comms import Comms
from agent_comms.execution.child_process import (
    NoResidentManager,
    Platform,
    ProcessIdentity,
    ResidentManager,
)
from agent_comms.registry.thread_identity import ThreadIncarnation, ThreadIncarnationRecord
from agent_comms.registry.threads import Thread
from agent_comms.storage.locked_store import DocumentChange
from agent_comms.task_attachments.owner_notices import BackgroundRunNotice
from agent_comms.transport.background_runs import (
    RETENTION,
    BackgroundRun,
    BackgroundRunStore,
    EndedState,
    ExitedOutcome,
    GoneOutcome,
    KilledOutcome,
    RaisedOutcome,
    ReturnedOutcome,
    RunningState,
    RunState,
    ShellRun,
    TellingState,
    ToldState,
    ToolCallRun,
    UntoldState,
)
from agent_comms.transport.tool_caller import ActsForTeam
from agent_comms.transport.tools.background import (
    CommsBackgroundStatusTool,
    CommsRunBackgroundTool,
)

THREAD = "PR111"


class ActingForFixer(ActsForTeam):
    """A session under a shared grant: its own thread, another thread's authority."""

    def authority(self, comms: Comms) -> Thread:
        return comms.registry.require("fixer")


CALLER = ActingForFixer(THREAD)
"""Who uses these tools: a session under a shared connector name. A caller that is its own
authority is the one arrangement that hides a run recorded under the wrong thread."""


def until[T](read: Callable[[], T], holds: Callable[[T], bool], seconds: float = 10.0) -> T:
    deadline = time.monotonic() + seconds
    while not holds(value := read()):
        assert time.monotonic() < deadline, f"still {value!r} after {seconds}s"
        time.sleep(0.02)
    return value


def store_of(comms: Comms) -> BackgroundRunStore:
    return BackgroundRunStore.of(comms)


def thread_of(comms: Comms) -> ThreadIncarnation:
    return comms.registry.require(THREAD).incarnation


def notices(comms: Comms) -> tuple[str, ...]:
    """The run notices delivered to the thread, as its inbox holds them."""
    return tuple(record.body for record in comms.bus.bus.inbox(THREAD, 0, 100)
                 if record.task == BackgroundRunNotice())


def stored(comms: Comms, run_id: str) -> BackgroundRun:
    return store_of(comms).read().runs[run_id]


def told(comms: Comms, run_id: str) -> ToldState:
    """The run once its watcher has told the thread."""
    state = until(lambda: stored(comms, run_id).state, lambda state: not state.successors())
    assert isinstance(state, ToldState)
    return state


def left_by_dead_bridge[R: BackgroundRun](comms: Comms, run: R) -> R:
    """A run recorded by a bridge that then died: in the store, with no watcher here."""
    store_of(comms).update(lambda current: DocumentChange.to(replace(
        current, runs=current.runs | {run.run_id: run})))
    return run


def launched(comms: Comms, command: str, seconds: int = 60) -> ShellRun:
    return ShellRun.launched(
        store_of(comms).log_directory, thread_of(comms), command, seconds)


def ended(comms: Comms, command: str = "true") -> ShellRun:
    """A shell run whose command has exited, as its launch left it."""
    run = launched(comms, command)
    until(lambda: run.exit_file.exists(), bool)
    return run


def dead_process() -> ProcessIdentity:
    process = subprocess.Popen(["/bin/true"])
    identity = Platform.current().identity(process.pid)
    process.wait()
    return identity


def status(comms: Comms, run_id: str = ""):
    return CommsBackgroundStatusTool(thread=THREAD, run_id=run_id).apply(
        comms, CALLER)


def test_a_shell_run_tells_its_thread_the_exit_code_and_output(wired: Comms) -> None:
    started = CommsRunBackgroundTool(thread=THREAD, command="echo out; exit 3").apply(
        wired, CALLER)

    outcome = told(wired, started.run_id).outcome

    assert isinstance(outcome, ExitedOutcome) and outcome.code == 3
    (notice,) = notices(wired)
    assert f"Background run {started.run_id} exited with code 3." in notice
    assert "command: echo out; exit 3" in notice and notice.rstrip().endswith("out")


def test_a_run_past_its_deadline_is_killed_and_says_so(wired: Comms) -> None:
    started = CommsRunBackgroundTool(thread=THREAD, command="sleep 60", seconds=1).apply(
        wired, CALLER)

    assert isinstance(told(wired, started.run_id).outcome, KilledOutcome)
    process = stored(wired, started.run_id).process
    with suppress(ChildProcessError):  # the killed leader is this test's child until reaped
        os.waitpid(process.pid, 0)
    assert not Platform.current().matches(process)
    assert "hit its deadline and was killed" in notices(wired)[0]


@pytest.mark.parametrize("started", [
    lambda comms, caller: CommsRunBackgroundTool(thread=THREAD, command="true").apply(
        comms, caller).run_id,
    lambda comms, caller: CommsBackgroundStatusTool(thread=THREAD, background=True).replied(
        comms, caller)["run_id"],
], ids=["shell", "tool_call"])
def test_a_run_belongs_to_the_caller_that_started_it_not_the_thread_it_acts_for(
        wired: Comms, started) -> None:
    run_id = started(wired, CALLER)

    told(wired, run_id)
    assert stored(wired, run_id).thread == thread_of(wired) and len(notices(wired)) == 1
    assert [row.run.run_id for row in CommsBackgroundStatusTool(thread=THREAD).apply(
        wired, CALLER).runs] == [run_id]


def test_a_shell_run_is_launched_as_the_hosts_resident_manager_places_it(
        wired: Comms, monkeypatch: pytest.MonkeyPatch) -> None:
    """A bridge that is a systemd unit takes its cgroup with it when it restarts, so the
    launch is the resident manager's: its command and its environment, whatever the host."""

    class Placing(NoResidentManager):
        def command(self, command: tuple[str, ...]) -> tuple[str, ...]:
            return ("/usr/bin/env", "PLACED_BY=manager", *command)

        def handoff_environment(self, environment) -> dict[str, str]:
            return {**environment, "HANDED": "off"}

    monkeypatch.setattr(ResidentManager, "current", staticmethod(lambda: Placing()))
    try:
        run = ended(wired, "echo $PLACED_BY $HANDED")
    finally:
        Placing.withdraw()

    assert run.tail().strip() == "manager off"


def test_a_run_that_ended_while_the_bridge_was_down_is_told_by_the_next_touch(
        wired: Comms) -> None:
    run = left_by_dead_bridge(wired, ended(wired, "echo late; exit 7"))
    assert notices(wired) == () and isinstance(stored(wired, run.run_id).state, RunningState)

    (row,) = status(wired).runs

    assert isinstance(row.run.state, ToldState) and row.run.state.outcome.code == 7
    assert "exited with code 7" in notices(wired)[0]


def test_a_run_still_alive_after_a_restart_is_watched_again(wired: Comms) -> None:
    gate = Path(wired.root) / "gate"
    run = left_by_dead_bridge(
        wired, launched(wired, f"until [ -e {gate} ]; do sleep 0.05; done; exit 5"))

    (row,) = status(wired, run.run_id).runs
    assert isinstance(row.run.state, RunningState) and notices(wired) == ()

    gate.touch()
    outcome = told(wired, run.run_id).outcome
    assert isinstance(outcome, ExitedOutcome) and outcome.code == 5


def test_a_run_whose_process_vanished_without_an_exit_code_is_gone(wired: Comms) -> None:
    run = left_by_dead_bridge(wired, launched(wired, "sleep 60"))
    Platform.current().force_group(run.process)
    with suppress(ChildProcessError):
        os.waitpid(run.process.pid, 0)

    (row,) = status(wired).runs

    assert isinstance(row.run.state.outcome, GoneOutcome)
    assert "is gone" in notices(wired)[0]


def test_a_reused_pid_is_not_adopted_as_the_run(wired: Comms) -> None:
    """The record holds the process's birth, so this live process under the recorded pid of
    a dead run is neither waited on nor signalled."""
    own = ProcessIdentity.current()
    run = left_by_dead_bridge(wired, ShellRun(
        run_id="reused", thread=thread_of(wired), log=Path(wired.root) / "reused.log",
        command="true", seconds=0, started_at=time.time() - 10,
        process=replace(own, start_time=own.start_time + 1)))

    (row,) = status(wired, run.run_id).runs

    assert isinstance(row.run.state.outcome, GoneOutcome)


def test_a_background_tool_call_is_a_run_whose_result_is_its_output(wired: Comms) -> None:
    started = CommsBackgroundStatusTool(thread=THREAD, background=True).replied(
        wired, CALLER)

    assert isinstance(told(wired, started["run_id"]).outcome, ReturnedOutcome)
    assert isinstance(stored(wired, started["run_id"]), ToolCallRun)
    (notice,) = notices(wired)
    assert "tool: background_status" in notice and '"runs"' in notice
    assert [row.run.run_id for row in status(wired).runs] == [started["run_id"]]


def test_a_background_tool_call_that_raises_tells_the_failure(wired: Comms) -> None:
    started = CommsRunBackgroundTool(thread=THREAD, command=" ", background=True).replied(
        wired, CALLER)

    assert isinstance(told(wired, started["run_id"]).outcome, RaisedOutcome)
    assert "A background command cannot be empty." in notices(wired)[0]


def test_a_tool_call_whose_bridge_died_is_told_gone_not_running(wired: Comms) -> None:
    hosted = ToolCallRun.hosted(
        store_of(wired).log_directory, thread_of(wired), "comms_x", lambda: pytest.fail("ran"))
    del ToolCallRun.hosted_here[hosted.run_id]
    run = left_by_dead_bridge(wired, replace(hosted, process=dead_process()))

    (row,) = status(wired).runs

    assert row.run.run_id == run.run_id and isinstance(row.run.state.outcome, GoneOutcome)


def test_a_run_is_told_once_however_often_it_is_touched(wired: Comms) -> None:
    run = left_by_dead_bridge(wired, ended(wired))

    for _ in range(3):
        status(wired)

    assert isinstance(stored(wired, run.run_id).state, ToldState) and len(notices(wired)) == 1


def test_a_delivery_that_fails_raises_leaves_the_run_untold_and_the_next_touch_tells_it(
        wired: Comms, monkeypatch: pytest.MonkeyPatch) -> None:
    """Told means the notice is on the bus. A run is never marked told and then not told."""
    first, second = (left_by_dead_bridge(wired, ended(wired)) for _ in range(2))

    def refused(*args: object, **kwargs: object) -> None:
        raise ConnectionError("the bus refused the notice")

    with monkeypatch.context() as broken:
        broken.setattr(BackgroundRunNotice, "publish", refused)
        with pytest.raises(ConnectionError, match="the bus refused the notice"):
            status(wired)

    assert notices(wired) == ()
    assert {type(stored(wired, run.run_id).state) for run in (first, second)} == {UntoldState}

    status(wired)

    assert {type(stored(wired, run.run_id).state) for run in (first, second)} == {ToldState}
    assert len(notices(wired)) == 2


def test_a_telling_whose_teller_died_is_owed_again(wired: Comms) -> None:
    run = left_by_dead_bridge(wired, replace(ended(wired), state=TellingState(
        ExitedOutcome(time.time(), 0), dead_process())))

    (row,) = status(wired).runs

    assert row.run.run_id == run.run_id and isinstance(row.run.state, ToldState)
    assert len(notices(wired)) == 1


def test_a_telling_held_by_a_live_teller_is_left_to_it(wired: Comms) -> None:
    held = TellingState(ExitedOutcome(time.time(), 0), ProcessIdentity.current())
    run = left_by_dead_bridge(wired, replace(ended(wired), state=held))

    status(wired)
    store_of(wired).settle(run.run_id, EndedState(GoneOutcome(time.time())))

    assert stored(wired, run.run_id).state == held and notices(wired) == ()


def test_a_later_thread_of_the_same_name_does_not_inherit_the_run(wired: Comms) -> None:
    """The record holds the thread's birth: the run of a thread that no longer exists has
    no reader, so it is dropped with its files instead of told to its namesake."""
    earlier = ThreadIncarnationRecord(THREAD, 1.0).incarnation()
    run = left_by_dead_bridge(wired, replace(ended(wired), thread=earlier))

    assert status(wired).runs == () and notices(wired) == ()
    assert run.run_id not in store_of(wired).read().runs and not run.log.exists()


def test_a_told_run_past_retention_is_forgotten_with_its_files(wired: Comms) -> None:
    done = ExitedOutcome(time.time(), 0)
    old = left_by_dead_bridge(wired, replace(
        ended(wired), state=ToldState(done, time.time() - RETENTION - 1)))
    fresh = left_by_dead_bridge(wired, replace(ended(wired), state=ToldState(done, time.time())))

    rows = status(wired).runs

    assert [row.run.run_id for row in rows] == [fresh.run_id]
    assert not old.log.exists() and not old.exit_file.exists()


def test_the_states_a_run_may_take_next() -> None:
    assert RunState.transition_table() == {
        "running": frozenset({"ended", "untold"}),
        "ended": frozenset({"telling"}),
        "untold": frozenset({"telling"}),
        "telling": frozenset({"told", "untold"}),
        "told": frozenset(),
    }


def test_status_of_an_unknown_run_is_empty(wired: Comms) -> None:
    assert status(wired, "no-such-run").runs == ()


@pytest.mark.parametrize("tool", [CommsRunBackgroundTool, CommsBackgroundStatusTool])
def test_the_tools_are_served(tool) -> None:
    from agent_comms.transport.tools import served  # noqa: F401 - registers the tools

    assert tool.schema()["name"] == tool.declared_name
