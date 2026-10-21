"""Typed installer results use the real publisher and retained channel history."""
from pathlib import Path

import pytest

from agent_comms.cli_commands import HistoryCliCommand, InstallResult
from agent_comms.commander.config import NoConfigFile
from agent_comms.commander_heartbeat import CommanderHeartbeat
from agent_comms.field_codec import FieldCodec
from agent_comms.installer.backup import BackupReport, RestoreSpace, UpgradeRefused
from agent_comms.installer.carry import Carried
from agent_comms.installer.launch import InstallReport
from agent_comms.installer.upgrade import (
    BusReadBack, CarriedRun, CreatedRun, FreshRehearsal, InstalledUpgrade,
    InstallRolledBack, RefusedOwner, RehearsalFiles, RetiredRun, StartedOwner,
    StepRun, UnchangedRun, Upgrade,
)
from agent_comms.thread_execution import ExternalThreadExecution
from conftest import declare_test_bus


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
    RetiredRun(("connector",), "created route", ("worker",), Path("retired-bus")),
), ids=("carried", "unchanged", "created", "retired"))
def test_typed_results_publish_as_installer_and_survive_history_read(private_comms, live):
    comms = declare_test_bus(private_comms, channels=("comms",))
    report = InstallReport(comms.root)
    found = Upgrade("a" * 40, "b" * 40, comms.root, ("carry_order_scopes",))
    installed = InstalledUpgrade(
        found.source, found.target, found.root, found.pending, FreshRehearsal(),
        live, InstallResult(NoConfigFile(), {}),
    )
    # The wire codec still carries the same result family and fields after factoring.
    decoded = FieldCodec.decode(Upgrade, FieldCodec.encode(installed))
    assert decoded == installed
    report.starting(found)
    report.finished(decoded)
    messages = tuple(reversed(HistoryCliCommand(target="#comms").apply(comms).messages))
    assert len(messages) == 2
    assert tuple(message.sender for message in messages) == ("installer", "installer")
    assert all(found.target[:12] in message.body for message in messages)
    assert all(line in messages[1].body for line in live.lines())
    sender = comms.registry.require("installer")
    assert sender.execution is ExternalThreadExecution
    print("POSTED", *(message.body for message in messages), sep="\n")


def test_rollback_and_refusal_are_published_from_their_error_types(private_comms):
    comms = declare_test_bus(private_comms, channels=("comms",))
    error = UpgradeRefused("carry_order_scopes refused its rows")
    restored = InstallRolledBack(error, (StartedOwner("worker"),), "saved-original-bus")
    report = InstallReport(comms.root)
    report.failed(error)
    report.failed(restored)
    messages = tuple(reversed(HistoryCliCommand(target="#comms").apply(comms).messages))
    assert len(messages) == 2
    assert all(message.sender == "installer" for message in messages)
    assert all(line in messages[1].body for line in restored.lines())
    assert str(error) in messages[1].body
    print("POSTED", *(message.body for message in messages), sep="\n")


def test_services_share_registration_and_publication_failure_does_not_escape(private_comms, capsys):
    # No #comms channel exists: the real publisher refuses it after registering its sender.
    CommanderHeartbeat.register(private_comms.registry)
    report = InstallReport(private_comms.root)
    report.failed(UpgradeRefused("stopped before carrying"))
    original = private_comms.registry.require("installer")
    report.register(private_comms.registry)
    assert private_comms.registry.require("installer").incarnation == original.incarnation
    assert private_comms.registry.require(CommanderHeartbeat.sender).execution is ExternalThreadExecution
    assert capsys.readouterr().err
