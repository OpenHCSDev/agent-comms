"""An install takes back the built runtimes nothing holds, and only those."""

import json
import os
import sys
import time
from contextlib import contextmanager
from pathlib import Path

from agent_comms.execution.child_process import (
    ProcessIdentity,
    SpawnedProcess,
)
from agent_comms.execution.launch_runtime import LaunchRuntime
from agent_comms.execution.runtime_processes import (
    ImportingRuntimeProcess,
    PointerReference,
    RuntimeApplication,
    RuntimeProcess,
    RuntimeProcesses,
    UnattributedRuntimeProcess,
    UnreadableRuntimeProcess,
    UnretiredRuntimeProcess,
)
from agent_comms.installer.runtime_retention import (
    RefusedRetirement,
    RuntimeRetention,
    SweptRetirement,
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


@contextmanager
def importing_child(library: Path, staged: LaunchRuntime):
    """A live system interpreter importing from ``library``, and the real scan that saw it.

    Launched and read through `RuntimeProcesses.observe` rather than constructed, because
    a constructed outcome passes straight over the thing being tested: the scan deciding
    for itself what a process is holding.
    """
    child = SpawnedProcess.launch(
        (sys.executable, str(Path(__file__).with_name("runtime_process_importer.py")),
         "borrower_module"),
        env={**os.environ, "PYTHONPATH": str(library)},
    ).process
    try:
        deadline = time.monotonic() + 10
        while True:
            scan = RuntimeProcesses.observe(staged)
            if any(child.pid == outcome.pid for outcome in scan.outcomes
                   if isinstance(outcome, ImportingRuntimeProcess)):
                break
            assert time.monotonic() < deadline, "The scan never saw the importing child"
            time.sleep(0.1)
        yield scan
    finally:
        child.terminate()
        child.wait(timeout=10)


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


def test_a_runtime_a_live_process_imports_from_is_not_reclaimable(tmp_path):
    """Read by the real /proc scanner, not a constructed outcome.

    codex-latency reproduced this against the collector: a system interpreter launched
    with PYTHONPATH into a build, importing from it and staying alive, left the scan with
    nothing unread and that build reclaimable. Its command names no runtime, so only its
    environment says what it is holding.
    """
    runtimes = tmp_path / "runtimes"
    staged, borrowed = built(runtimes, "new"), built(runtimes, "borrowed")
    library = borrowed.prefix / "lib"
    library.mkdir()
    (library / "borrower_module.py").write_text("VALUE = 1\n")

    with importing_child(library, staged) as scan:
        retention = RuntimeRetention.after_install(
            staged, staged, scan, tmp_path / "no-bus", tmp_path / "no-spill")
        assert borrowed.prefix not in retention.reclaimable()
        assert borrowed.prefix in retention.held


def test_an_import_path_through_the_pointer_retires_nothing(tmp_path):
    """Read by the real /proc scanner. The same ambiguity, reached the other way.

    codex-latency pointed a live importer at `current/lib` after the pointer case was
    already closed for commands, and the collector called the pointed-at build
    reclaimable: the command path and the environment path each resolved a path to a
    build, and only one of them had learned that `current` resolves to none. They now ask
    one `RuntimeReference`.
    """
    runtimes = tmp_path / "runtimes"
    staged, borrowed = built(runtimes, "new"), built(runtimes, "borrowed")
    library = borrowed.prefix / "lib"
    library.mkdir()
    (library / "borrower_module.py").write_text("VALUE = 1\n")
    pointer = LaunchRuntime.pointer_in(runtimes.parent)
    pointer.symlink_to(borrowed.prefix, target_is_directory=True)

    with importing_child(pointer / "lib", staged) as scan:
        retention = RuntimeRetention.after_install(
            staged, staged, scan, tmp_path / "no-bus", tmp_path / "no-spill")
        outcome = retention.retire()

    assert isinstance(outcome, RefusedRetirement)
    assert "names the installation's pointer" in outcome.line()
    assert borrowed.prefix.is_dir()


def test_a_backup_pointer_it_cannot_read_retires_nothing(tmp_path):
    """A record that is present and will not decode is unknown, not absent. Reading it as
    absence would drop the only hold on the runtime that backup would restore, and the
    sweep would take exactly the rollback target it was meant to protect."""
    runtimes = tmp_path / "runtimes"
    staged, stale = built(runtimes, "new"), built(runtimes, "stale")
    root = attached_root(tmp_path, staged)
    spill = tmp_path / "spill"
    backup = taken_backup(spill, root, stale)
    (backup / "stores/.root-runtime.json").write_text("{not json")

    retention = RuntimeRetention.after_install(
        staged, staged, RuntimeProcesses(()), root, spill)

    outcome = retention.retire()
    assert isinstance(outcome, RefusedRetirement)
    assert "Unreadable runtime pointer" in outcome.line()
    assert stale.prefix.is_dir()


def test_a_root_that_published_nothing_is_not_an_unreadable_one(tmp_path):
    """Absence is a complete answer, and must not refuse: a bus root with no record has
    published no runtime, which is the ordinary state of a root the install just made."""
    runtimes = tmp_path / "runtimes"
    staged, stale = built(runtimes, "new"), built(runtimes, "stale")

    retention = RuntimeRetention.after_install(
        staged, staged, RuntimeProcesses(()), tmp_path / "unpublished", tmp_path / "no-spill")

    assert isinstance(retention.retire(), SweptRetirement)
    assert not stale.prefix.exists()


def test_a_process_invoked_through_the_pointer_retires_nothing(tmp_path):
    """`current` names whichever build was published when the process started, so once it
    has moved the command no longer says which. It holds one of them and will not say
    which, so none of them can be decided about."""
    runtimes = tmp_path / "runtimes"
    staged, stale = built(runtimes, "new"), built(runtimes, "stale")
    pointer = LaunchRuntime.pointer_in(runtimes.parent)

    retention = RuntimeRetention.after_install(
        staged, staged,
        RuntimeProcesses((UnattributedRuntimeProcess(
            5150, PointerReference(pointer / "bin/python")),)),
        tmp_path / "no-bus", tmp_path / "no-spill")

    outcome = retention.retire()
    assert isinstance(outcome, RefusedRetirement)
    assert "names the installation's pointer" in outcome.line()
    assert stale.prefix.is_dir()


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
