"""A fake runtimes tree with real process identities, no live runtime signals."""

import asyncio
import os
import subprocess
import sys
from contextlib import ExitStack

from agent_comms.launch_runtime import LaunchRuntime
from agent_comms.runtime_processes import RuntimeProcess, RuntimeProcesses, UnreadableRuntimeProcess


def stop(child):
    if child.poll() is None:
        child.terminate()
    child.wait(timeout=10)


def test_runtime_process_family(tmp_path):
    runtimes = tmp_path / "runtimes"
    old, target = (LaunchRuntime(runtimes / name / "bin/python") for name in ("old", "target"))
    for runtime in (old, target):
        runtime.interpreter.parent.mkdir(parents=True)
        runtime.interpreter.symlink_to(sys.executable)
    with ExitStack() as cleanup:
        children = []
        for runtime in (old, target):
            child = subprocess.Popen([str(runtime.interpreter), "-c", "import time; time.sleep(120)"])
            cleanup.callback(stop, child)
            children.append(child)
        snapshot = RuntimeProcesses.observe(target)
        (candidate,) = snapshot.candidates
        assert isinstance(candidate, RuntimeProcess)
        assert candidate.pid == children[0].pid
        assert candidate.prefix == old.prefix
        # Resolving the interpreter would collapse both runtimes onto the shared Python.
        assert old.interpreter.resolve() == target.interpreter.resolve()
        (outcome,) = asyncio.run(snapshot.retire(5))
        assert outcome.successful, outcome.line()
        assert str(children[0].pid) in outcome.line()
        children[0].wait(timeout=10)
        assert children[1].poll() is None
        assert not RuntimeProcesses.observe(target).candidates

    # A named process whose command line cannot be read is not silently discarded.
    proc = tmp_path / "proc"
    entry = proc / str(os.getpid())
    (entry / "cmdline").mkdir(parents=True)
    (unknown,) = RuntimeProcesses.observe(target, proc).candidates
    assert isinstance(unknown, UnreadableRuntimeProcess)
    assert unknown.pid == os.getpid()
    assert not asyncio.run(unknown.retire(0)).successful
