"""A fake runtimes tree with real applications; observations never signal them."""

import os
import subprocess
import sys
from contextlib import ExitStack
from pathlib import Path

from agent_comms.child_process import Platform, ProcessIdentity
from agent_comms.field_codec import FieldCodec
from agent_comms.launch_runtime import LaunchRuntime
from agent_comms.runtime_processes import (
    RuntimeProcessOutcome,
    RuntimeProcesses,
    UnreadableRuntimeProcess,
    UnretiredRuntimeProcess,
)


def stop(child):
    if child.poll() is None:
        child.terminate()
    child.wait(timeout=10)


def test_runtime_process_family(tmp_path, monkeypatch):
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
        assert not RuntimeProcesses.observe(target).outcomes
        # Observe from a sibling: these applications are not that observer's children.
        observed = subprocess.run(
            [sys.executable, "-c",
             "import json, sys; "
             "from agent_comms.field_codec import FieldCodec; "
             "from agent_comms.launch_runtime import LaunchRuntime; "
             "from agent_comms.runtime_processes import RuntimeProcesses; "
             "print(json.dumps(FieldCodec.encode(RuntimeProcesses.observe("
             "LaunchRuntime.from_interpreter(sys.argv[1])))))",
             str(target.interpreter)],
            check=True, capture_output=True, text=True,
            env=LaunchRuntime.running().child_environment(os.environ),
        )
        snapshot = FieldCodec.decode(RuntimeProcesses, FieldCodec.parse(observed.stdout))
        (outcome,) = snapshot.outcomes
        assert isinstance(outcome, UnretiredRuntimeProcess)
        assert outcome.process == ProcessIdentity.capture(children[0].pid)
        assert outcome.process.prefix == old.prefix
        assert outcome.process.application.process == ProcessIdentity.capture(os.getpid())
        assert outcome.process.application.name == Platform.current().process_name(os.getpid())
        # Resolving the interpreter would collapse both runtimes onto the shared Python.
        assert old.interpreter.resolve() == target.interpreter.resolve()
        assert str(children[0].pid) in outcome.line()
        assert outcome.process.application.describe() in outcome.line()
        # Neither discovery nor rendering gains the right to terminate an application.
        assert all(child.poll() is None for child in children)
        decoded = FieldCodec.decode(RuntimeProcesses, FieldCodec.encode(snapshot))
        assert decoded == snapshot
        assert decoded.lines() == (outcome.line(),)
        assert FieldCodec.decode(RuntimeProcessOutcome, FieldCodec.encode(outcome)) == outcome
        stop(children[0])  # Only its actual launcher closes this child.
        assert not RuntimeProcesses.observe(target).outcomes
        assert children[1].poll() is None

    # A named process whose command line cannot be read is not silently discarded.
    proc = tmp_path / "proc"
    entry = proc / str(os.getpid())
    entry.mkdir(parents=True)

    def denied_arguments(platform, process):
        raise PermissionError("process command line denied")

    with monkeypatch.context() as denied:
        denied.setattr(type(Platform.current()), "arguments", denied_arguments)
        (unknown,) = RuntimeProcesses.observe(target, proc).outcomes
    assert isinstance(unknown, UnreadableRuntimeProcess)
    assert unknown.pid == os.getpid()
    assert str(unknown.pid) in unknown.line()
    assert unknown.reason
    assert FieldCodec.decode(RuntimeProcessOutcome, FieldCodec.encode(unknown)) == unknown
