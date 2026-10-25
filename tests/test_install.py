"""agent-comms install: the bus it creates or practises on, and the route to it."""
from __future__ import annotations


import json
import os
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest
from agent_comms.field_codec import TurnId
from agent_comms.threads import ParkedThreadWorkState

from agent_comms.active_route import ActiveRoute, read_active_route, route_new_bus
from agent_comms.child_process import NoProcess, ProcessIdentity
from agent_comms.bus import Bus
from agent_comms.recorded_bus import RecordedBusIdentity
from bus_record_fixture import prepare_native_comms
from agent_comms.comms import wire
from agent_comms.commander.config import GivenRepos, NamedCommander
from agent_comms.registry_document import NoCommanderProject, RegisteredCommanderProject
from agent_comms.thread_identity import RecordedThreadIncarnation
from agent_comms.installer.refusals import UpgradeRefused
from agent_comms.installer.backup import bus_files
from agent_comms.installer.carry import CarryChain
from agent_comms.installer.practice import PracticeCopy
from agent_comms.locked_store import BusFile, RegistryFile, RootFile, SQLiteFile
from agent_comms.maintenance_barrier import MaintenanceBarrier, MaintenanceOperator
from agent_comms.threads import Thread

REPO = Path(__file__).resolve().parents[1]


def bus(directory: Path) -> tuple[Path, RecordedBusIdentity]:
    """An installed native root holding a real published message."""
    root = directory / "wire"
    comms = prepare_native_comms(wire(root))
    comms.channels.create_tag("team")
    comms.registry.declare(Thread("worker", frozenset({"team"}), str(directory)))
    comms.messaging.send("worker", "#team", "hello")
    return root, RecordedBusIdentity.capture(comms.bus.bus.path)


def test_fresh_install_is_excluded_by_every_declared_bus_input(private_directory):
    root = private_directory / "bus"
    root.mkdir(mode=0o700)
    assert BusFile.fresh(root)
    for owner, directory in BusFile.declarations_in(root):
        path = owner.path_in(directory)
        path.touch()
        assert not BusFile.fresh(root), owner
        path.unlink()
        path.symlink_to(root / "absent-input")
        assert not BusFile.fresh(root), owner
        path.unlink()
    Bus.install(Bus.path_in(root))
    assert not BusFile.fresh(root)


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
    files = RootFile.existing_in(root)
    declared = {str(path.relative_to(root)) for path in files}
    databases = {str(path.relative_to(root)) for path, owner in files.items()
                 if issubclass(owner, SQLiteFile)}
    assert databases

    practice = PracticeCopy(root, private_directory / 'practice/bus')
    practice.include(tuple(RootFile.existing_in(root)))
    copy = practice.path
    measurement = practice.measurement
    copied = {str(path.relative_to(copy)) for path in copy.rglob("*") if path.is_file()}
    # The copy's own registry writer takes its own lock.
    own_lock = {str(path.relative_to(copy))
                for path in RegistryFile.transient_paths(RegistryFile.path_in(copy))}
    assert copied - own_lock == declared - witnesses
    assert measurement.bytes > 0
    for name in databases:
        query = "SELECT name FROM sqlite_schema ORDER BY name"
        with (closing(sqlite3.connect(root / name)) as original,
              closing(sqlite3.connect(copy / name)) as practised):
            assert practised.execute(query).fetchall() == original.execute(query).fetchall()
    for name in copied - databases:
        assert (copy / name).read_bytes() == (root / name).read_bytes()
    assert bus_files(root) == saved


def test_a_message_sent_while_the_bus_is_copied_preserves_one_database_cut(
        private_directory, monkeypatch, capsys):
    # The live install of 0d81f55e2 refused its practice copy ("Private bus checkpoint
    # root/size changed."): the commander heartbeat appended between the log's copy and
    # its checkpoint's, so the copied checkpoint pointed past the end of the copied log.
    import threading

    from agent_comms.cli import main
    root, _ = bus(private_directory)
    (root / "storage.json").write_text(json.dumps({"floor_gib": 0}))
    bus_path = Bus.path_in(root)
    writer = threading.Thread(target=lambda: wire(root).messaging.send("worker", "#team", "late"))
    copy_file = Bus.copy_file

    def copy_then_write(cls, source, target):
        result = copy_file(source, target)
        if Path(source) == bus_path:
            writer.start()
            writer.join(timeout=30)
        return result

    monkeypatch.setattr(Bus, "copy_file", classmethod(copy_then_write))
    practice = PracticeCopy(root, private_directory / 'practice/bus')
    practice.include(tuple(RootFile.existing_in(root)))
    copy = practice.path
    monkeypatch.undo()
    writer.join(timeout=30)
    assert main(["--root", str(copy), "history", "--limit", "50"]) == 0
    assert '"failed"' not in capsys.readouterr().out
    assert Bus(Bus.path_in(copy)).revision() == 1
    assert Bus(bus_path).revision() == 2


def test_install_report_encodes_its_registered_harnesses() -> None:
    # The live install printed nothing: its report's field type had no codec.
    from agent_comms.installer.probes.harnesses import InstallResult
    from agent_comms.field_codec import FieldCodec

    for config in (RegisteredCommanderProject(RecordedThreadIncarnation("lead", 1.0), ()),
                   NoCommanderProject()):
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
    comms = wire(root)
    home = _harnessless_home(private_directory, monkeypatch)
    result = InstallCliCommand(project=private_directory).apply(comms)
    assert result.config == NoCommanderProject()
    assert not (root / "commander.json").exists()
    assert not (home / ".config/agent-comms/commander.json").exists()


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
    comms = wire(root)
    comms.registry.declare(Thread("lead", frozenset(), str(private_directory)))
    with pytest.raises(UpgradeRefused):
        InstallCliCommand(project=private_directory, **flags).apply(comms)
    assert not (root / "commander.json").exists() and not (home / ".config/agent-comms/commander.json").is_symlink()


def test_an_install_without_flags_keeps_native_commander_and_parked_state(
    private_directory, monkeypatch,
):
    from agent_comms.cli_commands import InstallCliCommand

    root = (private_directory / "wire").resolve()
    root.mkdir(mode=0o700)
    comms = wire(root)
    comms.registry.declare(Thread("lead", frozenset(), str(private_directory)))
    comms.registry.declare(Thread(
        "parked-unit", frozenset(), str(private_directory)))
    comms.registry.set_work_state("parked-unit", ParkedThreadWorkState())
    recorded = RegisteredCommanderProject(comms.registry.require("lead").incarnation, ())
    comms.registry.set_commander_project(recorded)
    home = _harnessless_home(private_directory, monkeypatch)
    result = InstallCliCommand(project=private_directory).apply(comms)
    assert result.config == recorded
    assert wire(root).registry.snapshot().commander_project == recorded
    assert not (root / "commander.json").exists()
    assert not (home / ".config/agent-comms/commander.json").exists()
    assert comms.registry.require("parked-unit").work_state == ParkedThreadWorkState()


def test_a_practice_copy_of_a_bus_without_messages_reads_its_history_twice(private_directory, capsys):
    # The laptop's install refused this read: rebuilding the copy's empty index sealed it
    # inside an open transaction, so the commit moved the file past its own seal.
    from agent_comms.cli import main

    root = private_directory / "wire"
    root.mkdir(mode=0o700)
    Bus.install(Bus.path_in(root))
    (root / "storage.json").write_text(json.dumps({"floor_gib": 0}))
    practice = PracticeCopy(root, private_directory / 'practice/bus')
    practice.include(tuple(RootFile.existing_in(root)))
    copy = practice.path
    for _ in range(2):
        assert main(["--root", str(copy), "history", "--limit", "50"]) == 0
        assert '"failed"' not in capsys.readouterr().out


def test_practice_copy_takes_turn_files_but_no_live_process(private_directory):
    root = private_directory / "bus"
    Bus.install(Bus.path_in(root))
    comms = wire(root)
    (root / "storage.json").write_text(json.dumps({"floor_gib": 0}))
    comms.registry.declare(Thread(
        "owner", frozenset(), str(root), process_identity=ProcessIdentity.capture(os.getpid())))
    lease = comms.agents.begin_turn("owner", TurnId('9b473845f684444f80a26586ae298d2b')).turn_lease
    turn = comms.registry.require("owner").turn_record.path
    assert turn in comms.registry.store.cut_paths

    practice = PracticeCopy(root, private_directory / 'practice/bus')
    practice.include(tuple(RootFile.existing_in(root)))
    copy = practice.path
    # Install 20's copy still named unit-signal's live pid 2128765: a start, stop or wake
    # on the copy aimed at the live unit. The copy keeps the turn file, never its owner.
    assert (copy / turn.relative_to(root)).read_bytes() == turn.read_bytes()
    copied = wire(copy).registry.require("owner")
    assert (copied.process_identity, copied.executing) == (NoProcess(), False)
    assert comms.registry.require("owner").turn_lease.identity == lease.identity
