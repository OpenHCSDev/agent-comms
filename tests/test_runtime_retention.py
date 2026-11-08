"""An install takes back the built runtimes nothing holds, and only those."""

import json
from pathlib import Path

from agent_comms.child_process import ProcessIdentity
from agent_comms.installer.runtime_retention import (
    RefusedRetirement,
    RuntimeRetention,
    SweptRetirement,
)
from agent_comms.launch_runtime import LaunchRuntime
from agent_comms.runtime_processes import (
    RuntimeApplication,
    RuntimeProcess,
    RuntimeProcesses,
    UnreadableRuntimeProcess,
    UnretiredRuntimeProcess,
)


def built(runtimes: Path, name: str, *, completed: bool = True) -> LaunchRuntime:
    """One prefix as a build leaves it: a bin/python and, once completed, its record."""
    runtime = LaunchRuntime(runtimes / name / "bin/python")
    runtime.interpreter.parent.mkdir(parents=True)
    runtime.interpreter.touch()
    if completed:
        (runtime.prefix / "build.json").write_text(
            json.dumps({"core_commit": "0" * 40, "packages": {}}))
    return runtime


def running(runtime: LaunchRuntime) -> UnretiredRuntimeProcess:
    """What the install's own process scan reports for an application still on ``runtime``."""
    identity = ProcessIdentity.capture(1)
    return UnretiredRuntimeProcess(RuntimeProcess(
        pid=identity.pid, start_time=identity.start_time, interpreter=runtime.interpreter,
        application=RuntimeApplication(identity, "an application")))


def attached_root(tmp_path: Path, runtime: LaunchRuntime) -> Path:
    root = tmp_path / "bus"
    root.mkdir()
    (root / ".root-runtime.json").write_text(json.dumps({"prefix": str(runtime.prefix)}))
    return root


def test_an_install_retires_the_built_runtimes_nothing_holds(tmp_path):
    """One prefix per (commit, lock) and nothing retiring them put 129 builds and 16 GB on
    one machine in six days, until an install refused to start its teammates for want of
    10 GiB. Each build is reproducible; a build something points at is not."""
    runtimes = tmp_path / "runtimes"
    staged, outgoing = built(runtimes, "new"), built(runtimes, "old")
    borrowed, held_by_root = built(runtimes, "borrowed"), built(runtimes, "rooted")
    unfinished = built(runtimes, "half", completed=False)
    stale = tuple(built(runtimes, f"stale-{index}") for index in range(3))

    retention = RuntimeRetention.after_install(
        staged, outgoing, RuntimeProcesses((running(borrowed),)),
        attached_root(tmp_path, held_by_root))

    assert set(retention.reclaimable()) == {runtime.prefix for runtime in stale}
    outcome = retention.retire()
    assert isinstance(outcome, SweptRetirement)
    assert len(outcome.retired) == 3
    assert not any(runtime.prefix.exists() for runtime in stale)
    for kept in (staged, outgoing, borrowed, held_by_root, unfinished):
        assert kept.prefix.is_dir(), kept.prefix.name


def test_a_process_scan_it_could_not_read_retires_nothing(tmp_path):
    """A process table this cannot read is not one it can prune against: the runtime behind
    an unreadable PID is exactly the one a sweep must not guess about."""
    runtimes = tmp_path / "runtimes"
    staged = built(runtimes, "new")
    stale = built(runtimes, "stale")

    retention = RuntimeRetention.after_install(
        staged, staged, RuntimeProcesses((UnreadableRuntimeProcess(4321, "denied"),)),
        tmp_path / "no-bus")

    assert stale.prefix in retention.reclaimable()
    outcome = retention.retire()
    assert isinstance(outcome, RefusedRetirement)
    assert "4321" in outcome.line()
    assert stale.prefix.is_dir()
