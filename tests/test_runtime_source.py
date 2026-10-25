"""Runtime identity is independent of the source its children inherit."""

import os
import subprocess
from pathlib import Path

import pytest

from agent_comms.installer.commands import InstallCommands
from agent_comms.installer.refusals import UpgradeRefused
from agent_comms.installer.upgrade import OwnersStopped, Stack
from agent_comms.launch_runtime import LaunchRuntime, RestoredRuntime, RunningRuntime, RunningSource
from agent_comms.maintenance_barrier import TargetRuntime


def test_runtime_family_shares_interpreter_identity():
    interpreter = LaunchRuntime.running().interpreter
    members = [member(interpreter) for member in
               (LaunchRuntime, RunningRuntime, RestoredRuntime, TargetRuntime)]
    assert all(left == right for left in members for right in members)
    assert len(set(members)) == 1
    assert members[0] != LaunchRuntime(interpreter.with_name("another-python"))


def test_only_running_runtime_passes_this_process_source_to_children():
    running = LaunchRuntime.running()
    selected = LaunchRuntime(running.interpreter)
    inherited = {**os.environ, "PYTHONPATH": "/not-the-selected-runtime"}
    clean = selected.environment(inherited)
    assert "PYTHONPATH" not in clean
    assert selected.child_environment(clean) == clean
    assert running.child_environment(clean) == RunningSource.current().environment(clean)
    source = subprocess.run(
        [str(running.interpreter), "-P", "-c",
         "import agent_comms; print(agent_comms.__file__)"],
        env=running.environment(inherited), capture_output=True, text=True,
        check=True, timeout=30,
    )
    assert Path(source.stdout.strip()).resolve() == RunningSource.current().root / "agent_comms/__init__.py"
    installed = subprocess.run(
        source.args, env=clean, capture_output=True, text=True, check=True, timeout=30,
    )
    assert Path(installed.stdout.strip()).resolve() != Path(source.stdout.strip()).resolve()


def test_restored_runtime_preserves_saved_environment():
    saved = {**os.environ, "PYTHONPATH": "/saved/original/source"}
    restored = RestoredRuntime(LaunchRuntime.running().interpreter)
    assert restored.child_environment(saved) == saved
    assert restored.environment(saved)["PYTHONPATH"] == saved["PYTHONPATH"]


def test_restart_refusal_is_retained_and_chained(comms, tmp_path):
    owners = OwnersStopped(stopped=["p717-owner-that-does-not-exist"])
    with InstallCommands() as commands:
        stack = Stack(Path(__file__).resolve().parents[1], commands, home=tmp_path,
                      runtime=LaunchRuntime.running())
        with pytest.raises(UpgradeRefused) as failed:
            with owners.give_back:
                owners.give_back.callback(owners.start_on, stack, comms.root)
                raise UpgradeRefused("original stop refusal")
    assert owners.started
    assert owners.started[0].name == owners.stopped[0]
    assert failed.value.__context__.args == ("original stop refusal",)
    with pytest.raises(UpgradeRefused) as recorded:
        owners.started[0].require_success(UpgradeRefused)
    assert str(failed.value) == str(recorded.value)
