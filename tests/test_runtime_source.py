"""Runtime identity is independent of the source its children inherit."""

import os
import subprocess
from pathlib import Path

import pytest

from agent_comms.execution.launch_runtime import (
    CheckoutSource,
    InstalledSource,
    LaunchRuntime,
    NoImportPath,
    RecordedRuntime,
    RestartEnvironment,
    RestoredRuntime,
    RunningRuntime,
    RunningSource,
    RuntimeReplacedError,
)
from agent_comms.installer.commands import InstallCommands
from agent_comms.installer.refusals import OwnersNotRestarted, UpgradeRefused
from agent_comms.installer.upgrade import OwnersStopped, Stack
from agent_comms.storage.maintenance_barrier import TargetRuntime


def test_runtime_family_recognizes_this_process():
    interpreter = LaunchRuntime.running().interpreter
    members = [member(interpreter) for member in
               (LaunchRuntime, RunningRuntime, RestoredRuntime, TargetRuntime)]
    assert all(member.runs_this_process for member in members)
    assert not LaunchRuntime(interpreter.with_name("another-python")).runs_this_process


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
    # An editable installation can resolve the same file without inheriting PYTHONPATH.
    # The exact environment assertions above protect source isolation in either layout.


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
        with pytest.raises(OwnersNotRestarted) as failed:
            with owners.given_back_on(stack, comms.root):
                raise UpgradeRefused("original stop refusal")
    assert owners.started
    assert owners.started[0].name == owners.stopped[0]
    assert failed.value.__cause__.args == ("original stop refusal",)
    with pytest.raises(UpgradeRefused) as recorded:
        owners.started[0].require_success(UpgradeRefused)
    assert str(failed.value.restart) == str(recorded.value)
    assert "original stop refusal" in str(failed.value)


def test_a_succeeding_install_restarts_its_owners_unless_taken_over(comms, tmp_path):
    with InstallCommands() as commands:
        stack = Stack(Path(__file__).resolve().parents[1], commands, home=tmp_path,
                      runtime=LaunchRuntime.running())
        owners = OwnersStopped(stopped=["p717-owner-that-does-not-exist"])
        with pytest.raises(UpgradeRefused) as refused:
            with owners.given_back_on(stack, comms.root):
                pass
        assert not isinstance(refused.value, OwnersNotRestarted)
        assert owners.started[0].name == owners.stopped[0]
        taken = OwnersStopped(stopped=["p717-owner-that-does-not-exist"])
        with taken.given_back_on(stack, comms.root):
            taken.retired()
        assert taken.started == ()


def test_a_recorded_target_keeps_the_import_path_its_capture_selected(tmp_path):
    """A queued restart rebuilds its runtime from the stored record. The capture projected the
    queuing process's import path (a checkout's root first); the relaunch must not strip it, or
    a checkout-run Core restarts its owner on whatever copy the interpreter has installed."""
    interpreter = LaunchRuntime.running().interpreter
    captured = RunningRuntime(interpreter).environment(
        {**os.environ, "PYTHONPATH": str(tmp_path / "unrelated")})
    recorded = RestartEnvironment.inherit(captured)
    relaunched = RecordedRuntime(interpreter, recorded.pythonpath).environment(
        {**os.environ, "PYTHONPATH": str(tmp_path / "the-queue-watcher's")})
    assert relaunched.get("PYTHONPATH") == captured.get("PYTHONPATH")
    installed = RecordedRuntime(interpreter, NoImportPath()).environment(
        {**os.environ, "PYTHONPATH": str(tmp_path / "stray")})
    assert "PYTHONPATH" not in installed


def test_an_install_replaces_the_runtime_of_an_installed_source_but_never_a_checkout(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    installed, checkout = InstalledSource(tmp_path / "site"), CheckoutSource(tmp_path / "tree")
    assert not installed.replaced_by_install(), "with nothing installed, nothing was replaced"
    pointer = LaunchRuntime.current_pointer()
    pointer.parent.mkdir(parents=True)
    pointer.symlink_to(LaunchRuntime.running().prefix, target_is_directory=True)
    assert not installed.replaced_by_install(), "the current runtime is this process's own"
    newer = tmp_path / "runtimes/core-newer"
    newer.mkdir(parents=True)
    pointer.unlink()
    pointer.symlink_to(newer, target_is_directory=True)
    assert installed.replaced_by_install()
    assert not checkout.replaced_by_install(), "a checkout runs its tree, whatever is installed"


def test_a_replaced_runtime_refuses_to_read_on(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    installed = InstalledSource(tmp_path / "site")
    installed.require_current()  # nothing installed: nothing replaced
    pointer = LaunchRuntime.current_pointer()
    pointer.parent.mkdir(parents=True)
    newer = tmp_path / "runtimes/core-newer"
    newer.mkdir(parents=True)
    pointer.symlink_to(newer, target_is_directory=True)
    with pytest.raises(RuntimeReplacedError):
        installed.require_current()
    CheckoutSource(tmp_path / "tree").require_current()
