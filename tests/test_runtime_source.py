"""Runtime identity is independent of the source its children inherit."""

import os
import subprocess
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

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
from agent_comms.execution.owner_lifecycle import OwnerLifecycle
from agent_comms.execution.owner_restart import OwnerRestartHandoff
from agent_comms.execution.systemd_unit import AgentsSlice
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


def _launch_wave_lifecycle(count: int, events: list[tuple[str, str]]):
    root = Path("/retained-root")
    threads = tuple(
        SimpleNamespace(
            name=f"owner-{index}",
            role=SimpleNamespace(require_executable=lambda _refusal: None),
            process_alive=False,
            require_process=lambda index=index: SimpleNamespace(pid=index + 100),
        )
        for index in range(count)
    )
    owners = tuple(
        SimpleNamespace(
            name=thread.name,
            launch=SimpleNamespace(process=SimpleNamespace(pid=index)),
            require_current=lambda _snapshot, thread=thread: (
                events.append(("validate", thread.name)) or thread
            ),
        )
        for index, thread in enumerate(threads)
    )
    registry = SimpleNamespace(
        snapshot=lambda: object(),
        require_member=lambda name, _member: threads[int(name.removeprefix("owner-"))],
        status=lambda _name: SimpleNamespace(
            holds_process=lambda: False, require_visible=lambda _refusal: None
        ),
    )
    lifecycle = SimpleNamespace(root=root, registry=registry)
    lifecycle.launch_waves = lambda items, launch: OwnerLifecycle.launch_waves(
        lifecycle, items, launch
    )
    handoff = OwnerRestartHandoff(str(root), owners, None, None, None, None)
    return handoff, lifecycle, threads


def test_a_retained_owner_cohort_resumes_under_its_launch_budget(monkeypatch):
    monkeypatch.setattr(AgentsSlice, "owner_initialization_capacity", 3)
    events = []
    handoff, lifecycle, _threads = _launch_wave_lifecycle(11, events)
    active = 0
    peak = 0

    @contextmanager
    def launch(thread, source):
        nonlocal active, peak
        events.append(("start", thread.name))
        active += 1
        peak = max(peak, active)
        try:
            yield SimpleNamespace(
                require_process=lambda: SimpleNamespace(pid=source.process.pid + 100)
            )
        finally:
            active -= 1
            events.append(("join", thread.name))

    results = handoff._launch(lifecycle, launch)
    names = tuple(f"owner-{index}" for index in range(11))
    assert peak == 3
    assert active == 0
    assert tuple(name for event, name in events if event == "validate") == names
    assert events.index(("validate", "owner-10")) < events.index(("start", "owner-0"))
    assert tuple(name for event, name in events if event == "start") == names
    assert sorted(name for event, name in events if event == "join") == sorted(names)
    assert events.index(("join", "owner-0")) < events.index(("start", "owner-3"))
    assert tuple(result.thread for result in results) == names
    assert tuple(result.previous_pid for result in results) == tuple(range(11))
    assert tuple(result.pid for result in results) == tuple(range(100, 111))


def test_the_normal_install_start_path_consumes_the_same_launch_budget(monkeypatch):
    monkeypatch.setattr(AgentsSlice, "owner_initialization_capacity", 3)
    events = []
    _handoff, lifecycle, threads = _launch_wave_lifecycle(11, events)
    active = 0
    peak = 0

    @contextmanager
    def launching_owner(thread, _binary, _arguments, *, launch_runtime):
        nonlocal active, peak
        assert launch_runtime.runs_this_process
        events.append(("start", thread.name))
        active += 1
        peak = max(peak, active)
        try:
            yield thread
        finally:
            active -= 1
            events.append(("join", thread.name))

    lifecycle.launching_owner = launching_owner
    names = tuple(thread.name for thread in threads)
    results = OwnerLifecycle.start_many(lifecycle, names)
    assert peak == 3
    assert active == 0
    assert tuple(name for event, name in events if event == "start") == names
    assert sorted(name for event, name in events if event == "join") == sorted(names)
    assert events.index(("join", "owner-0")) < events.index(("start", "owner-3"))
    assert tuple(result.thread for result in results) == names
    assert all(result.launched for result in results)


def test_a_failed_restart_wave_joins_admitted_owners_and_admits_no_successor(monkeypatch):
    monkeypatch.setattr(AgentsSlice, "owner_initialization_capacity", 3)
    events = []
    handoff, lifecycle, _threads = _launch_wave_lifecycle(11, events)
    dispatched = []
    joined = []

    @contextmanager
    def launch(thread, source):
        dispatched.append(thread.name)
        try:
            yield SimpleNamespace(
                require_process=lambda: SimpleNamespace(pid=source.process.pid + 100)
            )
        finally:
            joined.append(thread.name)
            if thread.name == "owner-4":
                raise RuntimeError("owner initialization failed")

    with pytest.raises(RuntimeError, match="owner initialization failed"):
        handoff._launch(lifecycle, launch)
    names = tuple(f"owner-{index}" for index in range(11))
    admitted = names[:6]
    assert tuple(name for event, name in events if event == "validate") == names
    assert tuple(dispatched) == admitted
    assert sorted(joined) == sorted(admitted)
    assert tuple(owner.name for owner in handoff.owners) == names


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
