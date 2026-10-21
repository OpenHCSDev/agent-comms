"""Typed installer lifetimes use the real publisher and retained channel history."""
from pathlib import Path
import subprocess
import sys

import pytest

from agent_comms.cli_commands import HistoryCliCommand, InstallResult
from agent_comms.child_process import ProcessIdentity
from agent_comms.cli_bridges.shared import (
    BridgeResume, OwnerExitedBridge, ReplacedBridge, ResumedBridge, SessionBridgeResumes,
    UnobservedBridge,
)
from agent_comms.commander.config import NoConfigFile
from agent_comms.commander_heartbeat import CommanderHeartbeat
from agent_comms.comms import Comms
from agent_comms.errors import UnknownChannelError
from agent_comms.field_codec import FieldCodec
from agent_comms.install_choices import LiveMode, UnnamedInstaller
from agent_comms.installer.backup import BackupReport, RestoreSpace, UpgradeRefused
from agent_comms.installer.carry import Carried
from agent_comms.installer.practice import practice_copy
from agent_comms.installer.upgrade import (
    BusReadBack, CarriedRun, CommsChannel, CrashedInstall, CreatedRun, FreshInstall,
    FreshRehearsal, InstalledUpgrade, InstallRolledBack, RefusedInstall, RefusedOwner,
    RehearsalFiles, RetiredRun, RunReport, StartedOwner, StepRun, TargetTree, UnchangedRun,
    Upgrade, upgrade,
)
from agent_comms.launch_runtime import LaunchRuntime
from agent_comms.thread_execution import ExternalThreadExecution
from conftest import declare_test_bus

REPOSITORY = Path(__file__).resolve().parents[1]


@pytest.fixture
def reported_install(private_comms, tmp_path):
    comms = declare_test_bus(private_comms, channels=("comms",))
    scratch = tmp_path / "install-scratch"
    scratch.mkdir()
    return comms, RunReport(CommsChannel(comms.root)), TargetTree(REPOSITORY, scratch, "b" * 40)


def messages(comms):
    return tuple(reversed(HistoryCliCommand(target="#comms").apply(comms).messages))


@pytest.mark.parametrize("bridge", (
    ResumedBridge("session", LaunchRuntime(Path("/old/bin/python")),
                  LaunchRuntime(Path("/new/bin/python"))),
    OwnerExitedBridge("session"),
    ReplacedBridge("session", ProcessIdentity(101, 10), ProcessIdentity(102, 20)),
    UnobservedBridge("session", "ConnectionResetError: peer closed the resume socket"),
))
def test_bridge_outcome_family_survives_the_run_codec_and_report(reported_install, bridge):
    comms, report, tree = reported_install
    result = InstalledUpgrade(
        "a" * 40, tree.revision, comms.root, (), FreshRehearsal(),
        UnchangedRun(bridges=(bridge,)), InstallResult(NoConfigFile(), {}),
    )
    decoded = FieldCodec.decode(Upgrade, FieldCodec.encode(result))
    assert decoded == result
    assert FieldCodec.decode(BridgeResume, FieldCodec.encode(bridge)) == bridge
    assert bridge.resume() is bridge
    with report.running(tree):
        report.complete(decoded)
    started, finished = messages(comms)
    assert started.body.startswith("Install starting\n")
    assert all(line in finished.body for line in bridge.lines())


@pytest.mark.parametrize("bridge,unresumed", (
    (ResumedBridge("session", LaunchRuntime(Path("/old/bin/python")),
                   LaunchRuntime(Path("/new/bin/python"))), False),
    (OwnerExitedBridge("session"), False),
    (ReplacedBridge("session", ProcessIdentity(101, 10), ProcessIdentity(102, 20)), True),
    (UnobservedBridge("session", "resume deadline expired"), True),
))
def test_only_a_bridge_that_did_not_come_back_fails_the_install(bridge, unresumed):
    bridges = SessionBridgeResumes(paused=[bridge])
    bridges.resume()
    assert bridges.outcomes == [bridge]
    if not unresumed:
        bridges.require_resumed()
        return
    with pytest.raises(ExceptionGroup) as caught:
        bridges.require_resumed()
    assert [str(error) for error in caught.value.exceptions] == list(bridge.lines())


@pytest.mark.parametrize("live", (
    CarriedRun(
        steps=(StepRun("carry_order_scopes", Carried(6)),),
        files=RehearsalFiles(3, 1, ("todos.sqlite3",), 1024),
        read=BusReadBack(2, 2, 1), stopped=("worker",), services=("connector",),
        started=(StartedOwner("worker"), RefusedOwner("busy", "already running")),
        restore_space=RestoreSpace(1024, 4096, 0.01),
        backup=BackupReport("saved-before-install", 1024), retired_backups=(),
    ),
    UnchangedRun(),
    CreatedRun(("connector",), "created route"),
    RetiredRun(services=("connector",), route="created route", stopped=("worker",),
               retired=Path("retired-bus")),
), ids=("carried", "unchanged", "created", "retired"))
def test_typed_results_publish_as_installer_and_survive_history_read(reported_install, live):
    comms, report, tree = reported_install
    installed = InstalledUpgrade(
        "a" * 40, tree.revision, comms.root, ("carry_order_scopes",), FreshRehearsal(),
        live, InstallResult(NoConfigFile(), {}),
    )
    decoded = FieldCodec.decode(Upgrade, FieldCodec.encode(installed))
    assert decoded == installed
    with report.running(tree):
        assert len(messages(comms)) == 1
        report.complete(decoded)
        assert len(messages(comms)) == 1  # Completion does not publish before cleanup.
    started, finished = messages(comms)
    assert (started.sender, finished.sender) == ("installer", "installer")
    assert started.body.startswith("Install starting\n")
    assert finished.body.startswith("Install finished\n")
    assert all(tree.revision[:12] in message.body for message in (started, finished))
    assert all(line in finished.body for line in live.lines())
    assert comms.registry.require("installer").execution is ExternalThreadExecution


@pytest.mark.parametrize("error,member", (
    (UpgradeRefused("carry_order_scopes refused its rows"), RefusedInstall),
    (RuntimeError("failure after publication"), CrashedInstall),
    (SystemExit("install stopped"), CrashedInstall),
    (KeyboardInterrupt("install interrupted"), CrashedInstall),
))
def test_install_failure_family_posts_once_and_preserves_the_exception(
        reported_install, error, member):
    comms, report, tree = reported_install
    with pytest.raises(type(error)) as caught:
        with report.running(tree):
            raise error
    assert caught.value is error
    failure = report.outcome
    assert isinstance(failure, member)
    started, failed = messages(comms)
    assert started.body.startswith("Install starting\n")
    assert failed.body.startswith("Install failed")
    assert all(line in failed.body for line in failure.lines())


def test_rollback_and_refusal_are_published_from_their_error_types(reported_install):
    comms, report, tree = reported_install
    error = UpgradeRefused("carry_order_scopes refused its rows")
    runtime = LaunchRuntime(Path("/saved-original-runtime/bin/python"))
    restored = InstallRolledBack(error, (StartedOwner("worker"),), "saved-original-bus",
                                runtime=runtime)
    with pytest.raises(InstallRolledBack) as caught:
        with report.running(tree):
            raise restored
    assert caught.value is restored
    started, failed = messages(comms)
    assert (started.sender, failed.sender) == ("installer", "installer")
    assert all(line in failed.body for line in restored.lines())
    assert str(error) in failed.body
    assert str(runtime.prefix) in failed.body
    completed = subprocess.run(
        [sys.executable, "-m", "agent_comms.cli", "--root", str(comms.root),
         "history", "--target", "#comms"],
        capture_output=True, text=True, timeout=30,
    )
    assert completed.returncode == 0, completed.stderr
    assert FieldCodec.parse(completed.stdout) == HistoryCliCommand(target="#comms").apply(comms).to_wire()


def test_real_practice_copy_failure_posts_its_source_and_reraises(reported_install, tmp_path):
    comms, report, tree = reported_install
    destination = tmp_path / "occupied-practice-copy"
    destination.write_text("Do not replace this file")
    with pytest.raises(FileExistsError) as caught:
        with report.running(tree):
            with practice_copy(comms.root, destination):
                pytest.fail("The occupied practice destination must not be entered")
    assert destination.read_text() == "Do not replace this file"
    started, failed = messages(comms)
    assert started.body.startswith("Install starting\n")
    assert f"Install failed: FileExistsError: {caught.value}" in failed.body
    assert "Source: src/agent_comms/installer/practice.py:" in failed.body
    print("P404 forced practice-copy result:", failed.body)


def test_upgrade_preflight_failure_is_inside_the_reporting_lifetime(reported_install, tmp_path):
    comms, report, tree = reported_install
    checkout = tmp_path / "not-a-checkout"
    checkout.mkdir()
    scratch = tree.scratch
    with pytest.raises(UpgradeRefused) as caught:
        upgrade(checkout, comms.root, tree.revision, UnnamedInstaller(), LiveMode(), False,
                tmp_path, scratch, lambda: InstallResult(NoConfigFile(), {}))
    started, failed = messages(comms)
    assert started.body.startswith("Install starting\n")
    assert all(line in failed.body for line in caught.value.lines())
    assert not scratch.exists()


def test_a_missing_result_is_a_reported_crash_not_a_silent_success(reported_install):
    comms, report, tree = reported_install
    with pytest.raises(RuntimeError, match="without returning its result"):
        with report.running(tree):
            pass
    started, failed = messages(comms)
    assert started.body.startswith("Install starting\n")
    assert "Install failed: RuntimeError: Install exited without returning its result" in failed.body
    assert "Source: src/agent_comms/installer/upgrade.py:" in failed.body


def test_publication_failure_raises_after_registering_the_sender(private_comms, tmp_path):
    CommanderHeartbeat.register(private_comms.registry)
    channel = CommsChannel(private_comms.root)
    report = RunReport(channel)
    scratch = tmp_path / "install-scratch"
    scratch.mkdir()
    with pytest.raises(UnknownChannelError):
        with report.running(TargetTree(REPOSITORY, scratch, "b" * 40)):
            pytest.fail("A refused start must not run the install body")
    assert not scratch.exists()
    original = private_comms.registry.require("installer")
    channel.register(private_comms.registry)
    assert private_comms.registry.require("installer").incarnation == original.incarnation
    assert private_comms.registry.require(CommanderHeartbeat.sender).execution is ExternalThreadExecution


def test_cleanup_failure_replaces_a_finished_result_before_the_terminal_post(
        reported_install, monkeypatch):
    comms, report, unadded = reported_install
    head = subprocess.run(["git", "-C", REPOSITORY, "rev-parse", "HEAD"], check=True,
                          capture_output=True, text=True).stdout.strip()
    tree = TargetTree(REPOSITORY, unadded.scratch, head)
    tree.add()  # Only an added tree's retirement runs git.
    with pytest.raises(FileNotFoundError):
        with report.running(tree):
            report.complete(Upgrade("a" * 40, tree.revision, comms.root, ()))
            monkeypatch.setenv("PATH", "")  # The external git process cannot start.
    monkeypatch.undo()
    subprocess.run(["git", "-C", REPOSITORY, "worktree", "prune"], check=True)
    assert not tree.scratch.exists()
    started, failed = messages(comms)
    assert started.body.startswith("Install starting\n")
    assert failed.body.startswith("Install failed: FileNotFoundError:")
    assert "Source: src/agent_comms/repo.py:" in failed.body


def test_a_fresh_root_posts_nothing_before_its_bus_and_its_result_after(tmp_path):
    """Nothing is at a fresh root, so the start has nowhere to go; the install creates the
    bus and its #comms, and the result lands there."""
    root = tmp_path / "fresh-bus"
    scratch = tmp_path / "install-scratch"
    scratch.mkdir()
    report = RunReport(FreshInstall.destination(root))
    with report.running(TargetTree(REPOSITORY, scratch, "b" * 40)):
        assert not root.exists()
        root.mkdir(mode=0o700)
        Comms(root).messaging.initialize_private_initial_protocol()
        report.opened(CommsChannel.created(root))
        report.complete(Upgrade("a" * 40, "b" * 40, root, ()))
    (finished,) = messages(Comms(root))
    assert finished.sender == "installer"
    assert finished.body.startswith("Install finished\n")


def test_a_fresh_install_that_fails_before_its_bus_raises_its_own_error(tmp_path):
    root = tmp_path / "fresh-bus"
    scratch = tmp_path / "install-scratch"
    scratch.mkdir()
    error = UpgradeRefused("the target tree has no stack")
    with pytest.raises(UpgradeRefused) as caught:
        with RunReport(FreshInstall.destination(root)).running(
                TargetTree(REPOSITORY, scratch, "b" * 40)):
            raise error
    assert caught.value is error
    assert not root.exists()
