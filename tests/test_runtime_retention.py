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


def running(executable: Path) -> UnretiredRuntimeProcess:
    """What the install's own process scan reports for an application still running
    ``executable``, which need not be a runtime's own bin/python."""
    identity = ProcessIdentity.capture(1)
    return UnretiredRuntimeProcess(RuntimeProcess(
        pid=identity.pid, start_time=identity.start_time, interpreter=executable,
        application=RuntimeApplication(identity, "an application")))


def published(directory: Path, runtime: LaunchRuntime) -> Path:
    """A `.root-runtime.json` naming ``runtime``, as a bus root and a store backup hold it."""
    directory.mkdir(parents=True, exist_ok=True)
    (directory / ".root-runtime.json").write_text(json.dumps({"prefix": str(runtime.prefix)}))
    return directory


def attached_root(tmp_path: Path, runtime: LaunchRuntime) -> Path:
    return published(tmp_path / ".agent-comms", runtime)


def taken_backup(spill: Path, root: Path, runtime: LaunchRuntime) -> Path:
    """One retained store backup, named as BackupName writes it, holding the root's
    published runtime in its copied stores."""
    backup = spill / f"{root.name}.upgrade-20261107T223047"
    published(backup / "stores", runtime)
    return backup


def test_an_install_retires_the_built_runtimes_nothing_holds(tmp_path):
    """One prefix per (commit, lock) and nothing retiring them put 129 builds and 16 GB on
    one machine in six days, until an install refused to start its teammates for want of
    10 GiB. Each build is reproducible; a build something points at is not."""
    runtimes = tmp_path / "runtimes"
    staged, outgoing = built(runtimes, "new"), built(runtimes, "old")
    borrowed, bundled = built(runtimes, "borrowed"), built(runtimes, "bundled")
    held_by_root, restorable = built(runtimes, "rooted"), built(runtimes, "restorable")
    unfinished = built(runtimes, "half", completed=False)
    stale = tuple(built(runtimes, f"stale-{index}") for index in range(3))

    # A runtime bundles native children well below bin/: the grandparent of this one is a
    # directory inside the build, so holding it by that name would hold nothing real.
    native = bundled.prefix / "share/toad/neovim/bin/nvim"
    native.parent.mkdir(parents=True)
    native.touch()

    root = attached_root(tmp_path, held_by_root)
    spill = tmp_path / "spill"
    taken_backup(spill, root, restorable)

    retention = RuntimeRetention.after_install(
        staged, outgoing, RuntimeProcesses((running(borrowed.interpreter), running(native))),
        root, spill)

    assert set(retention.reclaimable()) == {runtime.prefix for runtime in stale}
    outcome = retention.retire()
    assert isinstance(outcome, SweptRetirement)
    assert len(outcome.retired) == 3
    assert not any(runtime.prefix.exists() for runtime in stale)
    for kept in (staged, outgoing, borrowed, bundled, held_by_root, restorable, unfinished):
        assert kept.prefix.is_dir(), kept.prefix.name


def test_a_process_scan_it_could_not_read_retires_nothing(tmp_path):
    """A process table this cannot read is not one it can prune against: the runtime behind
    an unreadable PID is exactly the one a sweep must not guess about."""
    runtimes = tmp_path / "runtimes"
    staged = built(runtimes, "new")
    stale = built(runtimes, "stale")

    retention = RuntimeRetention.after_install(
        staged, staged, RuntimeProcesses((UnreadableRuntimeProcess(4321, "denied"),)),
        tmp_path / "no-bus", tmp_path / "no-spill")

    assert stale.prefix in retention.reclaimable()
    outcome = retention.retire()
    assert isinstance(outcome, RefusedRetirement)
    assert "4321" in outcome.line()
    assert stale.prefix.is_dir()
