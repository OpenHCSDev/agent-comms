"""Run-scoped installer reporting and typed results on a real private bus."""
from dataclasses import replace
from pathlib import Path
import os
import subprocess

import pytest

from agent_comms.child_process import ProcessIdentity
from agent_comms.cli_bridges.shared import (
    OwnerExitedBridge, ReplacedBridge, ResumedBridge, UnobservedBridge,
)
from agent_comms.cli_commands import HistoryCliCommand, InstallCliCommand, InstallResult
from agent_comms.commander.config import NoConfigFile
from agent_comms.comms import Comms
from agent_comms.field_codec import FieldCodec
from agent_comms.installer.backup import BackupReport, RestoreSpace, UpgradeRefused
from agent_comms.installer.carry import Carried
from agent_comms.installer.launch import InstallReport, InstallRun
from agent_comms.installer.outcome import CrashedInstall, FailedInstall
from agent_comms.installer.reporting import InstalledWriter, PreparedFreshWriter
from agent_comms.installer.upgrade import (
    BusReadBack, CarriedRun, CreatedRun, FreshRehearsal, IncompleteRun, InstalledUpgrade,
    RefusedOwner, RehearsalFiles, RetiredRun, StartedOwner,
    StepRun, UnchangedRun, Upgrade, upgrade,
)
from agent_comms.launch_runtime import LaunchRuntime
from agent_comms.thread_execution import ExternalThreadExecution
from conftest import declare_test_bus


@pytest.mark.parametrize("live_factory", (
    pytest.param(lambda root: CarriedRun(
        steps=(StepRun("carry_order_scopes", Carried(6)),),
        files=RehearsalFiles(3, 1, ("todos.sqlite3",), 1024),
        read=BusReadBack(2, 2, 1), root=root, stopped=("worker",), services=("connector",),
        started=(StartedOwner("worker"), RefusedOwner("busy", "already running")),
        restore_space=RestoreSpace(1024, 4096, 0.01),
        backup=BackupReport("saved-before-install", 1024), retired_backups=(),
    ), id="carried"),
    pytest.param(lambda root: UnchangedRun(), id="unchanged"),
    pytest.param(lambda root: IncompleteRun(), id="incomplete"),
    pytest.param(lambda root: CreatedRun(("connector",), "created route"), id="created"),
    pytest.param(lambda root: RetiredRun(
        services=("connector",), route="created route", stopped=("worker",),
        retired=Path("retired-bus")), id="retired"),
))
def test_live_result_family_survives_codec_and_retained_history(report_bus, live_factory):
    comms = report_bus
    outgoing = LaunchRuntime(Path("/outgoing/bin/python"))
    target = LaunchRuntime(Path("/target/bin/python"))
    owner = ProcessIdentity.capture(os.getpid())
    successor = ProcessIdentity.capture(os.getppid())
    live = replace(live_factory(comms.root), bridges=(
        ResumedBridge("resumed", outgoing, target), OwnerExitedBridge("exited"),
        ReplacedBridge("replaced", owner, successor), UnobservedBridge("unobserved"),
    ))
    installed = InstalledUpgrade(
        "a" * 40, "b" * 40, comms.root, ("carry_order_scopes",), FreshRehearsal(),
        live, InstallResult(NoConfigFile(), {}),
    )
    decoded = FieldCodec.decode(Upgrade, FieldCodec.encode(installed))
    assert decoded == installed
    InstallReport.register(comms.registry)
    comms.messaging.send(InstallReport.sender, f"#{InstallReport.channel}",
                         "\n".join(decoded.lines()))
    messages = HistoryCliCommand(target=f"#{InstallReport.channel}").apply(comms).messages
    assert len(messages) == 1
    assert all(line in messages[0].body for line in live.lines())
    assert comms.registry.require(InstallReport.sender).execution is ExternalThreadExecution


@pytest.fixture
def report_bus(comms, durable_home):
    """Reporting starts no native owner: use the ordinary bus, not a native-SDK fixture."""
    comms.messaging.initialize_private_initial_protocol()
    return declare_test_bus(comms, channels=(InstallReport.channel,))


@pytest.fixture
def refused_request(report_bus, tmp_path):
    """An actual ancestry refusal, before stack building or dependency work."""
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    git = ["git", "-C", str(checkout)]
    subprocess.run([*git, "init", "-q"], check=True)
    subprocess.run([*git, "-c", "user.name=Installer test", "-c", "user.email=test@example.invalid",
                    "commit", "-q", "--allow-empty", "-m", "old"], check=True)
    target = subprocess.check_output([*git, "rev-parse", "HEAD"], text=True).strip()
    subprocess.run([*git, "-c", "user.name=Installer test", "-c", "user.email=test@example.invalid",
                    "commit", "-q", "--allow-empty", "-m", "new"], check=True)
    comms = report_bus
    request = InstallRun(InstallCliCommand(checkout=checkout, project=checkout),
                         comms.root, tmp_path / "scratch", target)
    return request, comms


@pytest.mark.parametrize("scratch_present,error_kind,outcome_kind", (
    (True, UpgradeRefused, UpgradeRefused),
    (False, FileNotFoundError, CrashedInstall),
), ids=("early-refusal", "cleanup-crash"))
def test_install_failure_family_through_upgrade(refused_request, scratch_present,
                                                error_kind, outcome_kind, durable_home):
    request, comms = refused_request
    if scratch_present:
        request.scratch.mkdir()
    report = InstallReport.for_run(request)
    with pytest.raises(error_kind) as raised:
        upgrade(request, report)
    messages = tuple(reversed(HistoryCliCommand(
        target=f"#{report.channel}").apply(comms).messages))
    assert len(messages) == 2
    assert messages[0].body.splitlines()[0] == "Install starting"
    assert messages[1].body.splitlines()[0].startswith("Install failed")
    assert all(message.sender == report.sender for message in messages)
    assert isinstance(report.outcome, FailedInstall)
    assert isinstance(report.outcome.failure, outcome_kind)
    assert report.outcome.failure.exception() is raised.value
    assert isinstance(report.writer, InstalledWriter)
    assert report.outgoing.home == durable_home
    assert not request.scratch.exists()
    assert all(line in messages[1].body for line in report.outcome.lines())
    assert report.writer.stack.runtime == LaunchRuntime.running()


@pytest.mark.parametrize("fresh,writer_kind", (
    (False, InstalledWriter), (True, PreparedFreshWriter),
), ids=("outgoing-schema", "fresh-source"))
def test_root_writer_family_through_upgrade(refused_request, fresh, writer_kind):
    request, _existing = refused_request
    root = request.root.with_name("fresh") if fresh else request.root
    request = replace(request, root=root)
    request.scratch.mkdir()
    report = InstallReport.for_run(request)
    with pytest.raises(UpgradeRefused):
        upgrade(request, report)
    assert isinstance(report.writer, writer_kind)
    messages = tuple(reversed(HistoryCliCommand(
        target=report.target).apply(Comms(root)).messages))
    assert len(messages) == 2
    assert messages[0].body.splitlines()[0] == "Install starting"
    assert messages[1].body.splitlines()[0].startswith("Install failed")
    assert not request.scratch.exists()
