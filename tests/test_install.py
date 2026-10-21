"""agent-comms install: the bus it creates or practises on, and the route to it."""
from __future__ import annotations

from agent_comms.bus import Bus

import json
import os
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest

from agent_comms.active_route import ActiveRoute, read_active_route, route_new_bus
from agent_comms.child_process import ProcessIdentity
from agent_comms.bus import Bus
from agent_comms.comms import Comms, wire
from agent_comms.commander.config import (
    CommanderConfig, GivenRepos, NamedCommander, NoConfigFile, WrittenConfigFile,
)
from agent_comms.installer.backup import UpgradeRefused, bus_files
from agent_comms.installer.carry import CarryChain, CarryLedger
from agent_comms.installer.practice import declared_files, practice_copy
from agent_comms.locked_store import SQLiteFile
from agent_comms.maintenance_barrier import MaintenanceBarrier, MaintenanceOperator
from agent_comms.wire_log import WireLog

REPO = Path(__file__).resolve().parents[1]


def bus(directory: Path) -> tuple[Path, str]:
    """A private bus holding one thread and one message, as production writes one."""
    root = directory / "wire"
    root.mkdir(mode=0o700)
    Bus.install(Bus.path_in(root))
    comms = Comms(root)
    comms.registry.declare(comms.registry.new_thread("worker", frozenset({"team"}), str(directory)))
    root_id = comms.messaging.initialize_private_initial_protocol()
    comms.messaging.send("worker", "#team", "hello")
    return root, root_id


def test_a_bus_install_creates_is_fresh_until_marked_and_runs_no_carry(private_directory):
    root = private_directory / "bus"
    root.mkdir(mode=0o700)
    log = WireLog(WireLog.path_in(root))
    assert log.fresh()
    Bus.install(Bus.path_in(root))
    assert not log.fresh()
    chain = CarryChain.at(REPO)
    CarryLedger(root).start(chain)
    assert chain.pending(CarryLedger(root).applied()) == ()


def test_install_routes_a_new_bus_and_replaces_only_a_route_that_selects_none(private_directory):
    first, first_id = bus(private_directory)
    (private_directory / "second").mkdir()
    second, second_id = bus(private_directory / "second")
    directory = private_directory / "route-state"
    directory.mkdir(mode=0o700)
    route_file = directory / "active-route.json"

    route_new_bus(ActiveRoute(first, first_id), route_file)
    assert read_active_route(route_file) == ActiveRoute(first, first_id)
    route_new_bus(ActiveRoute(second, second_id), route_file)
    assert read_active_route(route_file) == ActiveRoute(first, first_id)

    first.rename(first.with_name("retired"))
    # A route whose bus is gone still decodes, so an explicit --root is never refused by it.
    assert read_active_route(route_file) == ActiveRoute(first, first_id)
    route_new_bus(ActiveRoute(second, second_id), route_file)
    assert read_active_route(route_file) == ActiveRoute(second, second_id)
    (backup,) = directory.glob("active-route.*.bak.json")
    assert json.loads(backup.read_text())["root"] == str(first)


def test_practice_copy_is_the_declared_files_without_its_maintenance_witnesses(private_directory):
    root, _ = bus(private_directory)
    (root / "storage.json").write_text(json.dumps({"floor_gib": 0}))
    barrier = MaintenanceBarrier(root / "registry.json")
    barrier.begin(MaintenanceOperator.current())
    witnesses = {str(path.relative_to(root)) for path in barrier.witness_paths if path.exists()}
    assert witnesses
    # Install 31 died opening a native writer's transient claim that vanished after the
    # listing: an undeclared file has no owner, so the copy never meets it.
    (root / "sessions").mkdir(exist_ok=True)
    (root / "sessions/.pr48-writer-transient").write_text("claim")
    saved = bus_files(root)
    files = declared_files(root)
    declared = {str(path.relative_to(root)) for path in files}
    databases = {str(path.relative_to(root)) for path, owner in files.items()
                 if issubclass(owner, SQLiteFile)}
    assert databases

    with practice_copy(root, private_directory / "practice/bus") as (copy, measurement):
        copied = {str(path.relative_to(copy)) for path in copy.rglob("*") if path.is_file()}
        assert copied == declared - witnesses
        assert measurement.bytes > 0
        for name in databases:
            query = "SELECT name FROM sqlite_schema ORDER BY name"
            with (closing(sqlite3.connect(root / name)) as original,
                  closing(sqlite3.connect(copy / name)) as practised):
                assert practised.execute(query).fetchall() == original.execute(query).fetchall()
        for name in copied - databases:
            assert (copy / name).read_bytes() == (root / name).read_bytes()
    assert bus_files(root) == saved


def test_a_message_sent_while_the_bus_is_copied_never_splits_the_log_from_its_checkpoint(
        private_directory, monkeypatch, capsys):
    # The live install of 0d81f55e2 refused its practice copy ("Private bus checkpoint
    # root/size changed."): the commander heartbeat appended between the log's copy and
    # its checkpoint's, so the copied checkpoint pointed past the end of the copied log.
    import shutil
    import threading

    from agent_comms.cli import main
    from agent_comms.installer import practice
    from agent_comms.private_bus_checkpoint import WritableCheckpoint

    root, _ = bus(private_directory)
    (root / "storage.json").write_text(json.dumps({"floor_gib": 0}))
    log = WireLog(WireLog.path_in(root))
    writer = threading.Thread(target=lambda: Comms(root).messaging.send("worker", "#team", "late"))
    copy2, connect = shutil.copy2, sqlite3.connect

    def copy_then_write(source, target, *args, **kwargs):
        result = copy2(source, target, *args, **kwargs)
        if Path(source) == log.path:
            writer.start()
        return result

    def connect_after_writer(database, *args, **kwargs):
        if (str(WritableCheckpoint.path_in(root)) in str(database)
                and threading.current_thread() is not writer):
            writer.join(timeout=2)  # a writer held off by the bus lock stays out of the cut
        return connect(database, *args, **kwargs)

    monkeypatch.setattr(practice.shutil, "copy2", copy_then_write)
    monkeypatch.setattr(practice.sqlite3, "connect", connect_after_writer)
    with practice_copy(root, private_directory / "practice/bus") as (copy, _):
        monkeypatch.undo()
        writer.join(timeout=30)
        assert main(["--root", str(copy), "history", "--limit", "50"]) == 0
        assert '"failed"' not in capsys.readouterr().out


def test_install_report_encodes_its_registered_harnesses() -> None:
    # The live install printed nothing: its report's field type had no codec.
    from agent_comms.cli_commands import InstallResult
    from agent_comms.field_codec import FieldCodec

    for config in (WrittenConfigFile("/config.json"), NoConfigFile()):
        report = InstallResult(config, {"claude-code": ("skill", "tools")})
        assert FieldCodec.decode(InstallResult, FieldCodec.encode(report)) == report


def _harnessless_home(directory: Path, monkeypatch) -> Path:
    home = directory / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("PATH", "")
    return home


def test_an_install_on_a_bus_with_no_commander_writes_no_commander_config(private_directory, monkeypatch):
    # The laptop's install failed its last step: the flag default named this machine's commander.
    from agent_comms.cli_commands import InstallCliCommand

    root = private_directory / "wire"
    root.mkdir(mode=0o700)
    comms = Comms(root)
    home = _harnessless_home(private_directory, monkeypatch)
    result = InstallCliCommand(project=private_directory).apply(comms)
    assert result.config == NoConfigFile()
    assert not CommanderConfig.root_path(root).exists()
    assert not CommanderConfig.path(home).exists() and not CommanderConfig.path(home).is_symlink()


@pytest.mark.parametrize("flags", [
    {"commander": NamedCommander("lead")},
    {"repos": GivenRepos((Path("/checkout"),))},
], ids=["commander-without-repos", "repos-without-commander"])
def test_an_install_refuses_flags_a_bus_without_a_commander_record_cannot_use(
    private_directory, monkeypatch, flags,
):
    from agent_comms.cli_commands import InstallCliCommand

    root = private_directory / "wire"
    root.mkdir(mode=0o700)
    home = _harnessless_home(private_directory, monkeypatch)
    with pytest.raises(UpgradeRefused):
        InstallCliCommand(project=private_directory, **flags).apply(Comms(root))
    assert not CommanderConfig.root_path(root).exists() and not CommanderConfig.path(home).is_symlink()


def test_an_install_without_flags_keeps_the_recorded_commander_and_rehomes_its_paths(
    private_directory, monkeypatch,
):
    from agent_comms.cli_commands import InstallCliCommand

    root = (private_directory / "wire").resolve()
    root.mkdir(mode=0o700)
    comms = Comms(root)
    comms.registry.declare(comms.registry.new_thread("lead", frozenset(), str(private_directory)))
    old_home = private_directory / "old-home"
    recorded = CommanderConfig.on_machine("lead", (), root, old_home, frozenset({"parked-unit"}))
    recorded.write(old_home)
    home = _harnessless_home(private_directory, monkeypatch)
    result = InstallCliCommand(project=private_directory).apply(comms)
    expected = CommanderConfig.on_machine("lead", (), root, home, frozenset({"parked-unit"}))
    assert result.config == WrittenConfigFile(str(CommanderConfig.path(home)))
    assert CommanderConfig.root_path(root).read_text() == expected.model_dump_json(indent=1) + "\n"
    assert CommanderConfig.read(CommanderConfig.path(home)) == expected


def test_a_practice_copy_of_a_bus_without_messages_reads_its_history_twice(private_directory, capsys):
    # The laptop's install refused this read: rebuilding the copy's empty index sealed it
    # inside an open transaction, so the commit moved the file past its own seal.
    from agent_comms.cli import main

    root = private_directory / "wire"
    root.mkdir(mode=0o700)
    Bus.install(Bus.path_in(root))
    (root / "storage.json").write_text(json.dumps({"floor_gib": 0}))
    with practice_copy(root, private_directory / "practice/bus") as (copy, _):
        for _ in range(2):
            assert main(["--root", str(copy), "history", "--limit", "50"]) == 0
            assert '"failed"' not in capsys.readouterr().out


def test_practice_copy_takes_turn_files_in_the_registry_cut(private_directory):
    root = private_directory / "bus"
    Bus.install(Bus.path_in(root))
    comms = wire(root)
    (root / "storage.json").write_text(json.dumps({"floor_gib": 0}))
    comms.registry.declare(comms.registry.new_thread(
        "owner", frozenset(), str(root), process_identity=ProcessIdentity.capture(os.getpid())))
    lease = comms.agents.begin_turn("owner", "turn").turn_lease
    turn = comms.registry.require("owner").turn_record.path
    assert turn in comms.registry.store.cut_paths

    with practice_copy(root, private_directory / "practice/bus") as (copy, _):
        copied = Comms(copy).registry.require("owner")
        assert copied.turn_lease.identity == lease.identity
        assert copied.turn_record.read().executing
