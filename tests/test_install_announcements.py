"""Typed installer results use the real publisher and retained channel history."""
import sys
from dataclasses import replace
from agent_comms.cli_bridges.shared import UnobservedBridge
from pathlib import Path

import pytest

from agent_comms.channel_targets import CommsTag
from agent_comms.cli import build_parser
from agent_comms.cli_commands import BusReadBack, CliCommand, HistoryCliCommand
from agent_comms.installer.probes.harnesses import InstallResult
from agent_comms.registry_document import NoCommanderProject
from agent_comms.repo import GitCommandError
from agent_comms.commander_heartbeat import CommanderHeartbeat
from agent_comms.field_codec import FieldCodec
from agent_comms.host_services import RestartedService
from agent_comms.installer.backup import BackupReport, RestoreSpace
from agent_comms.installer.refusals import UpgradeRefused
from agent_comms.installer.probes.carry import Carried
from agent_comms.installer.launch import InstallReport
from agent_comms.installer.commands import InstallCommands, TimedCommand
from agent_comms.installer.interpreter_operation import InstalledProgram
from agent_comms.installer.probes.report import InstallReportProbe
from agent_comms.installer.reporting import FinishedInstall, RuntimeWriter
from agent_comms.installer.upgrade import CarriedPass, CarriedRun, CreatedRun, FreshRehearsal, InstalledUpgrade, RehearsalFiles, RetiredRun, StepRun, Stack, TargetTree, UnchangedRun, UnresumedUpgrade, Upgrade, upgrade
from agent_comms.installer.refusals import InstallRolledBack
from agent_comms.installer.owner_starts import RefusedOwner, StartedOwner
from agent_comms.install_choices import LiveMode, RehearsalMode, UnnamedInstaller
from agent_comms.launch_runtime import LaunchRuntime, RestoredRuntime
from agent_comms.thread_execution import ExternalThreadExecution
from cli_command import core
from conftest import declare_test_bus
from source_revision_fixture import SourceRepository


RESTARTED = RestartedService("Connector on port 1 for /bus", Path("/runtime/bin/python"))


@pytest.mark.parametrize("flags, mode, serialized", (
    ((), LiveMode, False), (("--rehearse",), RehearsalMode, True),
))
def test_install_mode_decodes_the_launchers_single_boolean_wire_form(flags, mode, serialized):
    arguments = build_parser().parse_args(("install", *flags))
    command = CliCommand.member_named(arguments.command).from_arguments(arguments)
    assert type(command.mode) is mode
    encoded = FieldCodec.encode(command)
    assert encoded["rehearse"] is serialized and "mode" not in encoded
    assert FieldCodec.decode(type(command), encoded) == command


def test_failed_rehearsal_does_not_publish_to_the_source_bus(private_comms, tmp_path):
    source = SourceRepository.create(tmp_path / "source")
    target = source.git(source.checkout, "rev-parse", "HEAD").strip()
    (source.checkout / "owner.py").write_text("# Tracked local change refuses the rehearsal.\n")
    before_bus = private_comms.bus.bus.revision()
    before_registry = private_comms.registry.snapshot()

    with pytest.raises(UpgradeRefused, match="local changes"):
        upgrade(
            source.checkout, private_comms.root, target, UnnamedInstaller(), RehearsalMode(), False,
            tmp_path / "spill", tmp_path / "scratch",
            lambda stack: pytest.fail("A refused rehearsal cannot install harnesses"),
            InstallReport(),
        )

    assert private_comms.bus.bus.revision() == before_bus
    assert private_comms.registry.snapshot() == before_registry


@pytest.mark.parametrize("live", (
    CarriedRun(
        steps=(StepRun("carry_order_scopes", Carried(6)),),
        files=RehearsalFiles(3, 1, (Path("todos.sqlite3"),), 1024),
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
    found = Upgrade("a" * 40, "b" * 40, comms.root, ("carry_order_scopes",))
    installed = InstalledUpgrade(
        found.source, found.target, found.root, found.pending, FreshRehearsal(),
        live, InstallResult(NoCommanderProject(), {}),
        commands=(TimedCommand("uv sync", 61.2), TimedCommand("git merge", 0.1)),
    )
    # The wire codec still carries the same result family and fields after factoring.
    decoded = FieldCodec.decode(Upgrade, FieldCodec.encode(installed))
    assert decoded == installed
    InstallReportProbe(comms.root, "\n".join(("Install starting", *found.lines()))).run()
    InstallReportProbe(comms.root, "\n".join(FinishedInstall(decoded).lines())).run()
    messages = tuple(reversed(HistoryCliCommand(target="#comms").apply(comms).messages))
    assert len(messages) == 2
    assert tuple(message.sender for message in messages) == ("installer", "installer")
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
    InstallReportProbe(comms.root, "\n".join(error.lines())).run()
    InstallReportProbe(comms.root, "\n".join(restored.lines())).run()
    messages = tuple(reversed(HistoryCliCommand(target="#comms").apply(comms).messages))
    assert len(messages) == 2
    assert all(message.sender == "installer" for message in messages)
    assert all(line in messages[1].body for line in restored.lines())
    assert str(error) in messages[1].body
    assert str(runtime.prefix) in messages[1].body
    completed = core("--root", str(comms.root), "history", "#comms")
    assert completed.returncode == 0, completed.stderr
    assert FieldCodec.parse(completed.stdout) == HistoryCliCommand(target="#comms").apply(comms).to_wire()


def test_a_fresh_bus_carries_the_installer_report_into_its_declared_channel(private_comms):
    # No thread carries the comms tag and nobody created it: CommsTag declares the channel.
    CommanderHeartbeat.register(private_comms.registry)
    probe = InstallReportProbe(private_comms.root, "\n".join(
        UpgradeRefused("stopped before carrying").lines()))
    probe.run()
    history = HistoryCliCommand(target=CommsTag.channel()).apply(private_comms).messages
    assert len(history) == 1 and history[0].body == probe.body
    original = private_comms.registry.require("installer")
    probe.register(private_comms.registry)
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
        InstallResult(NoCommanderProject(), {}), commands=(),
        bridge_resumes=(UnobservedBridge("copied-session", "new runtime absent"),),
    )
    InstallReportProbe(comms.root, "\n".join(FinishedInstall(unresumed).lines())).run()
    body = HistoryCliCommand(target=CommsTag.channel()).apply(comms).messages[0].body
    assert body.startswith(UnresumedUpgrade.finished)
    assert "Session bridge copied-session: new runtime was not observed" in body


@pytest.mark.parametrize("failure", ("none", "work", "cleanup"))
def test_terminal_report_follows_real_tree_cleanup_and_keeps_original_error(
        private_comms, tmp_path, failure, monkeypatch):
    comms = declare_test_bus(private_comms, channels=("comms",))
    source = SourceRepository.create(tmp_path / "source")
    target = source.main_commit
    tree = TargetTree(source.checkout, tmp_path / "scratch", target)
    tree.add()
    # This source test selects its source interpreter explicitly. The installed journey
    # separately qualifies artifact-only imports and the predecessor runtime.
    repository = Path(__file__).resolve().parents[1]
    monkeypatch.setenv("PYTHONPATH", str(repository / "src"))
    with InstallCommands() as commands:
        stack = Stack(repository, commands,
                      runtime=RestoredRuntime(Path(sys.executable)))
        writer = RuntimeWriter(comms.root, stack)
        report = InstallReport()
        refusal = UpgradeRefused("operation refused before its result")
        installed = InstalledUpgrade(
            "a" * 40, target, comms.root, (), FreshRehearsal(), UnchangedRun(),
            InstallResult(NoCommanderProject(), {}), commands=(),
        )
        observed = None
        try:
            with report.running(tree):
                report.starting(writer, target)
                if failure == "work":
                    raise refusal
                if failure == "cleanup":
                    source.git(source.checkout, "worktree", "lock", str(tree.path))
                published = replace(stack, program=InstalledProgram())
                report.writer = RuntimeWriter(comms.root, published)
                report.complete(installed)
        except (UpgradeRefused, GitCommandError) as error:
            observed = error
        finally:
            if tree.path.exists():
                source.git(source.checkout, "worktree", "unlock", str(tree.path))
                tree.__exit__()
    messages = tuple(reversed(HistoryCliCommand(target=CommsTag.channel()).apply(comms).messages))
    assert len(messages) == 2 and messages[0].body.startswith("Install starting")
    assert not tree.scratch.exists()
    if failure == "none":
        assert observed is None
        assert messages[1].body.startswith("Install finished")
    else:
        assert observed is not None
        assert messages[1].body.startswith("Install failed")
        assert "Install finished" not in messages[1].body
        if failure == "work":
            assert observed is refusal
        else:
            assert isinstance(observed, GitCommandError)
