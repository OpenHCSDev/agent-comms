"""Background runs: one family for shell commands and deferred tool calls, settled by one
watcher, reported once, and recovered after the bridge that started them is gone."""

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
from agent_comms.storage.locked_store import DocumentChange
from agent_comms.task_attachments.owner_notices import BackgroundRunNotice
from agent_comms.transport.background_runs import (
    RETENTION,
    BackgroundRun,
    BackgroundRunStore,
    ExitedState,
    GoneState,
    KilledState,
    RaisedState,
    ReturnedState,
    RunningState,
    RunState,
    ShellRun,
    ToolCallRun,
)
from agent_comms.transport.tool_caller import ThreadCaller
from agent_comms.transport.tools.background import (
    CommsBackgroundStatusTool,
    CommsRunBackgroundTool,
)

THREAD = "PR111"


def until[T](read: Callable[[], T], holds: Callable[[T], bool], seconds: float = 10.0) -> T:
    deadline = time.monotonic() + seconds
    while not holds(value := read()):
        assert time.monotonic() < deadline, f"still {value!r} after {seconds}s"
        time.sleep(0.02)
    return value


def notices(comms: Comms) -> tuple[str, ...]:
    """The run notices delivered to the thread, as its inbox holds them."""
    return tuple(record.body for record in comms.bus.bus.inbox(THREAD, 0, 100)
                 if record.task == BackgroundRunNotice())


def stored(comms: Comms, run_id: str) -> BackgroundRun:
    return BackgroundRunStore.at(comms.root).read().runs[run_id]


def reported(comms: Comms, run_id: str) -> BackgroundRun:
    return until(lambda: stored(comms, run_id), lambda run: run.reported and bool(notices(comms)))


def left_by_dead_bridge(comms: Comms, run: BackgroundRun) -> BackgroundRun:
    """A run recorded by a bridge that then died: in the store, with no watcher in this process."""
    BackgroundRunStore.at(comms.root).update(lambda current: DocumentChange.to(replace(
        current, runs=current.runs | {run.run_id: run})))
    return run


def launched(comms: Comms, command: str, seconds: int = 60) -> ShellRun:
    return ShellRun.launched(
        BackgroundRunStore.at(comms.root).log_directory, THREAD, command, seconds)


def dead_process() -> ProcessIdentity:
    process = subprocess.Popen(["/bin/true"])
    identity = Platform.current().identity(process.pid)
    process.wait()
    return identity


def status(comms: Comms, run_id: str = ""):
    return CommsBackgroundStatusTool(thread=THREAD, run_id=run_id).apply(
        comms, ThreadCaller(THREAD))


def test_a_shell_run_reports_its_exit_code_and_output_to_its_thread(wired: Comms) -> None:
    started = CommsRunBackgroundTool(thread=THREAD, command="echo out; exit 3").apply(
        wired, ThreadCaller(THREAD))

    run = reported(wired, started.run_id)

    assert isinstance(run.state, ExitedState) and run.state.code == 3
    (notice,) = notices(wired)
    assert f"Background run {started.run_id} exited with code 3." in notice
    assert "command: echo out; exit 3" in notice and notice.rstrip().endswith("out")


def test_a_run_past_its_deadline_is_killed_and_says_so(wired: Comms) -> None:
    started = CommsRunBackgroundTool(thread=THREAD, command="sleep 60", seconds=1).apply(
        wired, ThreadCaller(THREAD))

    run = reported(wired, started.run_id)

    assert isinstance(run.state, KilledState)
    assert not Platform.current().matches(run.process) or _reaped(run.process)
    assert "hit its deadline and was killed" in notices(wired)[0]


def _reaped(process: ProcessIdentity) -> bool:
    """The killed leader is this test process's child until reaped."""
    with suppress(ChildProcessError):
        os.waitpid(process.pid, 0)
    return not Platform.current().matches(process)


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
        run = left_by_dead_bridge(wired, launched(wired, "echo $PLACED_BY $HANDED"))
    finally:
        Placing.withdraw()
    until(lambda: run.exit_file.exists(), bool)

    assert run.tail().strip() == "manager off"


def test_a_run_that_ended_while_the_bridge_was_down_is_reported_at_the_next_touch(
        wired: Comms) -> None:
    run = left_by_dead_bridge(wired, launched(wired, "echo late; exit 7"))
    until(lambda: run.exit_file.exists(), bool)
    assert notices(wired) == () and isinstance(stored(wired, run.run_id).state, RunningState)

    (row,) = status(wired).runs

    assert isinstance(row.run.state, ExitedState) and row.run.state.code == 7 and row.run.reported
    assert "exited with code 7" in notices(wired)[0]


def test_a_run_still_alive_after_a_restart_is_watched_again(wired: Comms) -> None:
    gate = Path(wired.root) / "gate"
    run = left_by_dead_bridge(
        wired, launched(wired, f"until [ -e {gate} ]; do sleep 0.05; done; exit 5"))

    (row,) = status(wired, run.run_id).runs
    assert isinstance(row.run.state, RunningState) and notices(wired) == ()

    gate.touch()
    ended = reported(wired, run.run_id)
    assert isinstance(ended.state, ExitedState) and ended.state.code == 5


def test_a_run_whose_process_vanished_without_an_exit_code_is_gone(wired: Comms) -> None:
    run = left_by_dead_bridge(wired, replace(launched(wired, "sleep 60"), run_id="vanished"))
    Platform.current().force_group(run.process)

    status(wired)

    assert isinstance(reported(wired, run.run_id).state, GoneState)
    assert "is gone" in notices(wired)[0]


def test_a_reused_pid_is_not_adopted_as_the_run(wired: Comms) -> None:
    """The record holds the process's birth, so this live process under the recorded pid of
    a dead run is neither waited on nor signalled."""
    own = ProcessIdentity.current()
    reused = replace(own, start_time=own.start_time + 1)
    run = left_by_dead_bridge(wired, ShellRun(
        run_id="reused", thread=THREAD, log=str(Path(wired.root) / "reused.log"),
        command="true", seconds=0, process=reused, started_at=time.time() - 10))

    status(wired)

    assert isinstance(reported(wired, run.run_id).state, GoneState)


def test_a_background_tool_call_is_a_run_whose_result_is_its_output(wired: Comms) -> None:
    started = CommsBackgroundStatusTool(thread=THREAD, background=True).replied(
        wired, ThreadCaller(THREAD))

    run = reported(wired, started["run_id"])

    assert isinstance(run, ToolCallRun) and isinstance(run.state, ReturnedState)
    (notice,) = notices(wired)
    assert "tool: background_status" in notice and '"runs"' in notice
    assert [row.run.run_id for row in status(wired).runs] == [run.run_id]


def test_a_background_tool_call_that_raises_reports_the_failure(wired: Comms) -> None:
    started = CommsRunBackgroundTool(thread=THREAD, command=" ", background=True).replied(
        wired, ThreadCaller(THREAD))

    run = reported(wired, started["run_id"])

    assert isinstance(run.state, RaisedState)
    assert "A background command cannot be empty." in notices(wired)[0]


def test_a_tool_call_whose_bridge_died_is_reported_gone_not_running(wired: Comms) -> None:
    hosted = ToolCallRun.hosted(BackgroundRunStore.at(wired.root).log_directory, THREAD, "comms_x")
    run = left_by_dead_bridge(wired, replace(hosted, process=dead_process()))

    status(wired)

    assert isinstance(reported(wired, run.run_id).state, GoneState)


def test_an_ended_run_is_reported_once_however_often_it_is_touched(wired: Comms) -> None:
    run = left_by_dead_bridge(wired, replace(
        launched(wired, "true"), state=ExitedState(at=time.time(), code=0)))

    for _ in range(3):
        status(wired)

    assert stored(wired, run.run_id).reported and len(notices(wired)) == 1


def test_an_ended_run_keeps_the_state_it_ended_in(wired: Comms) -> None:
    store = BackgroundRunStore.at(wired.root)
    run = left_by_dead_bridge(wired, replace(
        launched(wired, "true"), state=KilledState(at=1.0), reported=True))

    store.settle(run.run_id, GoneState(at=2.0))

    assert stored(wired, run.run_id).state == KilledState(at=1.0)
    assert RunState.transition_table()["running"] == frozenset(
        {"exited", "killed", "returned", "raised", "gone"})


def test_a_reported_run_past_retention_is_forgotten_with_its_files(wired: Comms) -> None:
    old = time.time() - RETENTION - 1
    run = launched(wired, "true")
    until(lambda: run.exit_file.exists(), bool)
    left_by_dead_bridge(wired, replace(run, state=ExitedState(at=old, code=0), reported=True))
    fresh = left_by_dead_bridge(wired, replace(
        launched(wired, "true"), state=ExitedState(at=time.time(), code=0), reported=True))

    rows = status(wired).runs

    assert [row.run.run_id for row in rows] == [fresh.run_id]
    assert not Path(run.log).exists() and not run.exit_file.exists()


def test_status_of_an_unknown_run_is_empty(wired: Comms) -> None:
    assert status(wired, "no-such-run").runs == ()


@pytest.mark.parametrize("tool", [CommsRunBackgroundTool, CommsBackgroundStatusTool])
def test_the_tools_are_served(tool) -> None:
    from agent_comms.transport.tools import served  # noqa: F401 - registers the tools

    assert tool.schema()["name"] == tool.declared_name
