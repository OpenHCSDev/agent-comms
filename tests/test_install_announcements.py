"""Typed installer results use the real publisher and retained channel history."""
import subprocess
import sys
from pathlib import Path

import pytest

from agent_comms.channel_targets import CommsTag
from agent_comms.cli_commands import BusReadBack, HistoryCliCommand, InstallResult
from agent_comms.commander.config import NoConfigFile
from agent_comms.commander_heartbeat import CommanderHeartbeat
from agent_comms.field_codec import FieldCodec
from agent_comms.host_services import RestartedService
from agent_comms.installer.backup import BackupReport, RestoreSpace, UpgradeRefused
from agent_comms.installer.carry import Carried
from agent_comms.installer.launch import InstallReport
from agent_comms.installer.commands import TimedCommand
from agent_comms.installer.upgrade import (
    CarriedPass,
    CarriedRun,
    CreatedRun,
    FreshRehearsal,
    InstalledUpgrade,
    InstallRolledBack,
    RefusedOwner,
    RehearsalFiles,
    RetiredRun,
    StartedOwner,
    StepRun,
    UnchangedRun,
    UnresumedUpgrade,
    Upgrade,
    upgrade,
)
from agent_comms.install_choices import UnnamedInstaller
from agent_comms.launch_runtime import LaunchRuntime
from agent_comms.thread_execution import ExternalThreadExecution
from conftest import declare_test_bus
from source_revision_fixture import SourceRepository


RESTARTED = RestartedService("Connector on port 1 for /bus", Path("/runtime/bin/python"))


def test_failed_rehearsal_does_not_publish_to_the_source_bus(private_comms, tmp_path):
    source = SourceRepository.create(tmp_path / "source")
    target = source.git(source.checkout, "rev-parse", "HEAD").strip()
    (source.checkout / "owner.py").write_text("# Tracked local change refuses the rehearsal.\n")
    before_bus = private_comms.bus.bus.revision()
    before_registry = private_comms.registry.snapshot()

    with pytest.raises(UpgradeRefused, match="local changes"):
        upgrade(
            source.checkout, private_comms.root, target, UnnamedInstaller(), True, False,
            tmp_path / "spill", tmp_path / "scratch",
            lambda: pytest.fail("A refused rehearsal cannot install harnesses"),
            InstallReport(private_comms.root),
        )

    assert private_comms.bus.bus.revision() == before_bus
    assert private_comms.registry.snapshot() == before_registry


@pytest.mark.parametrize("live", (
    CarriedRun(
        steps=(StepRun("carry_order_scopes", Carried(6)),),
        files=RehearsalFiles(3, 1, ("todos.sqlite3",), 1024),
        read=BusReadBack(2), stopped=("worker",), services=(RESTARTED,),
        started=(StartedOwner("worker"), RefusedOwner("busy", "already running")),
        restore_space=RestoreSpace(1024, 4096, 0.01),
        backup=BackupReport("saved-before-install", 1024), retired_backups=(),
    ),
    UnchangedRun(),
    CreatedRun((RESTARTED,), "created route"),
    RetiredRun(services=(RESTARTED,), route="created route", stopped=("worker",),
               retired=Path("retired-bus")),
), ids=("carried", "unchanged", "created", "retired"))
def test_typed_results_publish_as_installer_and_survive_history_read(private_comms, live):
    comms = declare_test_bus(private_comms, channels=("comms",))
    report = InstallReport(comms.root)
    found = Upgrade("a" * 40, "b" * 40, comms.root, ("carry_order_scopes",))
    installed = InstalledUpgrade(
        found.source, found.target, found.root, found.pending, FreshRehearsal(),
        live, InstallResult(NoConfigFile(), {}),
        commands=(TimedCommand("uv sync", 61.2), TimedCommand("git merge", 0.1)),
    )
    # The wire codec still carries the same result family and fields after factoring.
    decoded = FieldCodec.decode(Upgrade, FieldCodec.encode(installed))
    assert decoded == installed
    started = report.starting(found)
    finished = report.finished(decoded)
    messages = tuple(reversed(HistoryCliCommand(target="#comms").apply(comms).messages))
    assert len(messages) == 2
    assert tuple(message.sender for message in messages) == ("installer", "installer")
    assert tuple(message.reference for message in messages) == (started.reference, finished.reference)
    assert all(found.target[:12] in message.body for message in messages)
    assert all(line in messages[1].body for line in live.lines())
    assert "Commands: 2 in 61.3s; slowest:\n61.2s uv sync\n0.1s git merge" in messages[1].body
    sender = comms.registry.require("installer")
    assert sender.execution is ExternalThreadExecution


def test_rollback_and_refusal_are_published_from_their_error_types(private_comms):
    comms = declare_test_bus(private_comms, channels=("comms",))
    error = UpgradeRefused("carry_order_scopes refused its rows")
    runtime = LaunchRuntime(Path("/saved-original-runtime/bin/python"))
    restored = InstallRolledBack(error, (StartedOwner("worker"),), "saved-original-bus",
                                runtime=runtime)
    report = InstallReport(comms.root)
    report.failed(error)
    report.failed(restored)
    messages = tuple(reversed(HistoryCliCommand(target="#comms").apply(comms).messages))
    assert len(messages) == 2
    assert all(message.sender == "installer" for message in messages)
    assert all(line in messages[1].body for line in restored.lines())
    assert str(error) in messages[1].body
    assert str(runtime.prefix) in messages[1].body
    completed = subprocess.run(
        [sys.executable, "-m", "agent_comms.cli", "--root", str(comms.root),
         "history", "--target", "#comms"],
        capture_output=True, text=True, timeout=30,
    )
    assert completed.returncode == 0, completed.stderr
    assert FieldCodec.parse(completed.stdout) == HistoryCliCommand(target="#comms").apply(comms).to_wire()


def test_a_fresh_bus_carries_the_installer_report_into_its_declared_channel(private_comms):
    # No thread carries the comms tag and nobody created it: CommsTag declares the channel.
    CommanderHeartbeat.register(private_comms.registry)
    report = InstallReport(private_comms.root)
    posted = report.failed(UpgradeRefused("stopped before carrying"))
    history = HistoryCliCommand(target=CommsTag.channel()).apply(private_comms).messages
    assert tuple(message.reference for message in history) == (posted.reference,)
    original = private_comms.registry.require("installer")
    report.register(private_comms.registry)
    assert private_comms.registry.require("installer").incarnation == original.incarnation
    assert private_comms.registry.require(CommanderHeartbeat.sender).execution is ExternalThreadExecution


def test_a_read_back_that_differs_from_before_the_carry_refuses():
    passed = CarriedPass(steps=(), files=RehearsalFiles.none(), read=BusReadBack(2))
    passed.require_read(BusReadBack(2))
    with pytest.raises(UpgradeRefused, match="before its carry it read"):
        passed.require_read(BusReadBack(3))


def test_an_install_whose_bridges_did_not_resume_says_so_in_its_headline(private_comms):
    comms = declare_test_bus(private_comms, channels=("comms",))
    unresumed = UnresumedUpgrade(
        "a" * 40, "b" * 40, comms.root, (), FreshRehearsal(), UnchangedRun(),
        InstallResult(NoConfigFile(), {}), ("Not resumed: Session bridge copied-session",),
        commands=(),
    )
    body = InstallReport(comms.root).finished(unresumed).body
    assert body.startswith(UnresumedUpgrade.finished)
    assert "Not resumed: Session bridge copied-session" in body
