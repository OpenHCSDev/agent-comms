"""A build outlives the process image that started it, and no longer outlives the install.

An install syncs its runtime, execs into that runtime, and completes the build from there.
No scope in one process spans those halves, so an interruption between them left a synced,
unrecorded prefix that no later install would touch: the retirement refuses to sweep a
build that might be in flight, and could not tell in-flight from abandoned.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from agent_comms.execution.child_process import ProcessIdentity
from agent_comms.execution.launch_runtime import LaunchRuntime
from agent_comms.execution.runtime_processes import RuntimeProcesses
from agent_comms.installer.building_runtime import BuildingRuntime
from agent_comms.installer.runtime_resolution import RuntimeResolution
from agent_comms.installer.runtime_retention import RuntimeRetention
from agent_comms.installer.runtime_stack import RuntimeStack


def unbuilt(runtimes: Path, name: str) -> LaunchRuntime:
    """A prefix as a sync leaves it: no recorded resolution."""
    runtime = LaunchRuntime(runtimes / name / "bin/python")
    runtime.interpreter.parent.mkdir(parents=True)
    runtime.interpreter.touch()
    return runtime


def abandon(prefix: Path) -> None:
    """Claim ``prefix`` from a real process and let that process exit, which is what an
    install killed between its sync and its completion leaves behind."""
    done = subprocess.run(
        (sys.executable, str(Path(__file__).with_name("runtime_build_abandoner.py")), str(prefix)),
        env={**os.environ, "PYTHONPATH": str(Path(__file__).parents[1] / "src")},
        check=True, capture_output=True)
    assert list(BuildingRuntime.claims_on(prefix)), done.stderr.decode(errors="replace")


def test_a_build_its_install_abandoned_is_reclaimable(tmp_path):
    """Its claim names a process that is gone, so the install that synced it is not coming
    back to complete it. Three of these were stranded on one machine, each held forever by
    the rule that protects a build in flight."""
    runtimes = tmp_path / "runtimes"
    staged, stranded = unbuilt(runtimes, "new"), unbuilt(runtimes, "stranded")
    abandon(stranded.prefix)

    retention = RuntimeRetention.after_install(
        staged, staged, RuntimeProcesses(()), tmp_path / "no-bus", tmp_path / "no-spill")

    assert stranded.prefix in retention.reclaimable()


def test_the_sync_is_claimed_before_it_can_leave_output(tmp_path):
    """A kill runs no handler, so what protects a prefix has to already be on disk when
    its first durable byte is. The double reads the claim from inside the sync, which is
    the moment an interrupted install stops.

    codex-latency found this: claiming after `uv sync` returned left a kill during the
    sync, which is most of its duration, stranding a prefix with no claim at all.
    """
    observed: dict[str, object] = {}
    prefix = tmp_path / "runtimes" / "syncing"

    class WatchedCommands:
        def run(self, *arguments, **keywords):
            observed["claims"] = [path.name for path in BuildingRuntime.claims_on(prefix)]
            observed["prefix_exists"] = prefix.is_dir()
            return ""

    stack = RuntimeStack(tmp_path / "checkout", WatchedCommands(), tmp_path,
                         runtime=LaunchRuntime.at_prefix(prefix))
    stack.sync()

    assert observed["claims"], "the sync ran with nothing claiming its prefix"
    assert observed["prefix_exists"], "the sync ran before its prefix existed"
    assert BuildingRuntime.abandoned(prefix) is False
    assert list(BuildingRuntime.claims_on(prefix))


def test_a_failed_sync_leaves_neither_prefix_nor_claim(tmp_path):
    """Its own failure is the one interruption it can handle, and it takes both."""
    prefix = tmp_path / "runtimes" / "failing"

    class FailingCommands:
        def run(self, *arguments, **keywords):
            raise RuntimeError("uv said no")

    stack = RuntimeStack(tmp_path / "checkout", FailingCommands(), tmp_path,
                         runtime=LaunchRuntime.at_prefix(prefix))
    with pytest.raises(RuntimeError):
        stack.sync()

    assert not prefix.exists()
    assert not list(BuildingRuntime.claims_on(prefix))


def test_one_builder_leaving_does_not_condemn_another_still_building(tmp_path):
    """Two installs of the same commit and lock build the same prefix name. With one
    claim file they would overwrite each other and either one dying would condemn the
    other's live build, so each writes its own name and a prefix is abandoned only when
    every builder that claimed it has gone."""
    runtimes = tmp_path / "runtimes"
    shared = unbuilt(runtimes, "shared")
    abandon(shared.prefix)
    BuildingRuntime.claim(shared.prefix)

    assert len(list(BuildingRuntime.claims_on(shared.prefix))) == 2
    assert not BuildingRuntime.abandoned(shared.prefix)

    BuildingRuntime.release(shared.prefix)
    abandon(shared.prefix)
    assert BuildingRuntime.abandoned(shared.prefix)


def test_a_build_in_flight_is_not_reclaimable(tmp_path):
    """Its claim names this process, which is running: the prefix is being built right now
    and taking it would pull the floor out from under the install doing it."""
    runtimes = tmp_path / "runtimes"
    staged, building = unbuilt(runtimes, "new"), unbuilt(runtimes, "building")
    BuildingRuntime.claim(building.prefix)

    retention = RuntimeRetention.after_install(
        staged, staged, RuntimeProcesses(()), tmp_path / "no-bus", tmp_path / "no-spill")

    assert building.prefix not in retention.reclaimable()


def test_a_prefix_with_no_claim_and_no_resolution_is_left_alone(tmp_path):
    """Unknown is not abandoned. A prefix from before builds recorded their builder says
    nothing about whether anyone is still constructing it."""
    runtimes = tmp_path / "runtimes"
    staged, older = unbuilt(runtimes, "new"), unbuilt(runtimes, "older")

    retention = RuntimeRetention.after_install(
        staged, staged, RuntimeProcesses(()), tmp_path / "no-bus", tmp_path / "no-spill")

    assert older.prefix not in retention.reclaimable()


def test_a_claim_that_will_not_read_is_left_alone(tmp_path):
    """An unreadable claim is not a dead one, and this decides a removal."""
    runtimes = tmp_path / "runtimes"
    staged, damaged = unbuilt(runtimes, "new"), unbuilt(runtimes, "damaged")
    abandon(damaged.prefix)
    next(BuildingRuntime.claims_on(damaged.prefix)).write_text("{not json")

    retention = RuntimeRetention.after_install(
        staged, staged, RuntimeProcesses(()), tmp_path / "no-bus", tmp_path / "no-spill")

    assert damaged.prefix not in retention.reclaimable()


def test_a_reused_pid_does_not_pass_for_its_builder(tmp_path):
    """The claim carries the kernel's start time beside the pid, so a pid that has come
    back as something else is a gone builder, not a live one."""
    runtimes = tmp_path / "runtimes"
    staged, stranded = unbuilt(runtimes, "new"), unbuilt(runtimes, "stranded")
    BuildingRuntime.claim(stranded.prefix)
    claim = next(BuildingRuntime.claims_on(stranded.prefix))
    record = json.loads(claim.read_text())
    record["builder"]["start_time"] = ProcessIdentity.capture(os.getpid()).start_time + 1
    claim.write_text(json.dumps(record))

    retention = RuntimeRetention.after_install(
        staged, staged, RuntimeProcesses(()), tmp_path / "no-bus", tmp_path / "no-spill")

    assert stranded.prefix in retention.reclaimable()


def test_a_completed_build_keeps_no_claim(tmp_path):
    """Completion is what ends the construction, so the claim goes with it rather than
    sitting in every built runtime naming a process that has long since exited."""
    prefix = tmp_path / "built"
    prefix.mkdir()
    BuildingRuntime.claim(prefix)
    BuildingRuntime.release(prefix)

    assert not list(BuildingRuntime.claims_on(prefix))
    assert not BuildingRuntime.abandoned(prefix)
    (prefix / "build.json").write_text(json.dumps({"core_commit": "0" * 40, "packages": {}}))
    assert RuntimeResolution.recorded(prefix).reclaimable()
