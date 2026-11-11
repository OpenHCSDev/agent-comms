"""agent-comms install: the bus it creates or practises on, and the route to it."""
from __future__ import annotations

import json
import os
import sqlite3
import sys
from contextlib import closing
from pathlib import Path

from agent_comms.comms import wire
from agent_comms.execution.child_process import (
    NoProcess,
    ProcessIdentity,
)
from agent_comms.installer.active_route import ActiveRoute, read_active_route, route_new_bus
from agent_comms.installer.backup import bus_files
from agent_comms.installer.practice import PracticeCopy
from agent_comms.messaging.bus import Bus
from agent_comms.messaging.recorded_bus import RecordedBusIdentity
from agent_comms.nominal.field_codec import TurnId
from agent_comms.projects.checkouts import Checkout
from agent_comms.registry.registry_document import NoTeamProject, RegisteredTeamProject
from agent_comms.registry.threads import NativeThread, ParkedThreadWorkState
from agent_comms.storage.locked_store import (
    BusFile,
    LiveFilesRequest,
    RegistryFile,
    RootFile,
    SQLiteFile,
)
from agent_comms.storage.maintenance_barrier import MaintenanceBarrier, MaintenanceOperator
from agent_comms.team.config import GivenRepos
from bus_record_fixture import prepare_native_comms

REPO = Path(__file__).resolve().parents[1]


def bus(directory: Path) -> tuple[Path, RecordedBusIdentity]:
    """An installed native root holding a real published message."""
    root = directory / "wire"
    comms = prepare_native_comms(wire(root))
    comms.channels.create_tag("team")
    comms.registry.declare(NativeThread("worker", frozenset({"team"}), Checkout(directory)))
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
    barrier = MaintenanceBarrier(RegistryFile.path_in(root))
    barrier.begin(MaintenanceOperator.current())
    witnesses = {str(path.relative_to(root)) for path in barrier.witness_paths if path.exists()}
    assert witnesses
    # Install 31 died opening a native writer's transient claim that vanished after the
    # listing: an undeclared file has no owner, so the copy never meets it.
    (root / "sessions").mkdir(exist_ok=True)
    (root / "sessions/.pr48-writer-transient").write_text("claim")
    saved = bus_files(LiveFilesRequest(root))
    files = RootFile.existing_in(root)
    declared = {str(path.relative_to(root)) for path in files}
    databases = {str(path.relative_to(root)) for path, owner in files.items()
                 if issubclass(owner, SQLiteFile)}
    assert databases

    practice = PracticeCopy(root, private_directory / 'practice/bus')
    practice.include(tuple(RootFile.existing_in(root)))
    copy = practice.path
    measurement = practice.measurement
    observed = {path: path.stat() for path in copy.rglob("*") if path.is_file()}
    # Each file owner distinguishes retained content from locks and SQLite read artifacts.
    noncontent = set()
    for path, owner in files.items():
        retained = copy / path.relative_to(root)
        noncontent.update(owner.member_paths(retained) - owner.snapshot_members(retained, observed))
    copied = {str(path.relative_to(copy)) for path in observed.keys() - noncontent}
    assert copied == declared - witnesses
    assert measurement.bytes > 0
    for name in databases:
        query = "SELECT name FROM sqlite_schema ORDER BY name"
        # Read-only: a writable last close would checkpoint and delete the source's WAL.
        with (closing(sqlite3.connect(f"file:{root / name}?mode=ro", uri=True)) as original,
              closing(sqlite3.connect(copy / name)) as practised):
            assert practised.execute(query).fetchall() == original.execute(query).fetchall()
    for name in copied - databases:
        assert (copy / name).read_bytes() == (root / name).read_bytes()
    assert bus_files(LiveFilesRequest(root)) == saved


def test_the_rehearsal_copy_holds_the_registry_with_its_presence_rows(private_directory):
    # Installs of 8921dfdb refused twice, 2026-11-06: once no pending carry named
    # presence.json, the selected copy held registry.json alone, and the installed runtime's
    # owners probe on that copy raised "a declared thread has no presence row".
    from agent_comms.installer.interpreter_operation import InstallClock
    from agent_comms.installer.upgrade import PracticeBus

    root, _ = bus(private_directory)
    (root / "storage.json").write_text(json.dumps({"floor_gib": 0}))
    copy = PracticeCopy(root, private_directory / "practice/bus")
    PracticeBus(private_directory / "practice", copy, root, private_directory / "spill",
                InstallClock()).select((), ())
    assert (wire(copy.path).registry.snapshot().threads.keys()
            == wire(root).registry.snapshot().threads.keys())


def test_a_message_sent_while_the_bus_is_copied_preserves_one_database_cut(
        private_directory, monkeypatch, capsys):
    # The live install of 0d81f55e2 refused its practice copy ("Private bus checkpoint
    # root/size changed."): the team heartbeat appended between the log's copy and
    # its checkpoint's, so the copied checkpoint pointed past the end of the copied log.
    import threading

    from agent_comms.cli import CliInvocation
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
    assert CliInvocation().invoke(["--root", str(copy), "history", "--limit", "50"], sys.stdout) == 0
    assert '"failed"' not in capsys.readouterr().out
    assert Bus(Bus.path_in(copy)).revision() == 1
    assert Bus(bus_path).revision() == 2


def test_install_report_encodes_its_registered_harnesses() -> None:
    # The live install printed nothing: its report's field type had no codec.
    from agent_comms.installer.probes.harnesses import InstallResult
    from agent_comms.nominal.field_codec import FieldCodec

    for config in (RegisteredTeamProject(()),
                   NoTeamProject()):
        report = InstallResult(config, {"claude-code": ("skill", "tools")})
        assert FieldCodec.decode(InstallResult, FieldCodec.encode(report)) == report


def _harnessless_home(directory: Path, monkeypatch) -> None:
    home = directory / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("PATH", "")


def test_an_install_on_a_bus_with_no_team_writes_no_team_config(private_directory, monkeypatch):
    # The laptop's install failed its last step: the flag default named this machine's colleague.
    from agent_comms.cli_verbs.install import InstallCliCommand

    root = private_directory / "wire"
    root.mkdir(mode=0o700)
    comms = wire(root)
    _harnessless_home(private_directory, monkeypatch)
    result = InstallCliCommand(project=private_directory).apply(comms)
    assert result.config == NoTeamProject()


def test_an_install_given_repos_founds_the_team_with_no_lead(private_directory, monkeypatch):
    # The team owns its board and repositories: founding it names no thread.
    from agent_comms.cli_verbs.install import InstallCliCommand

    root = private_directory / "wire"
    root.mkdir(mode=0o700)
    _harnessless_home(private_directory, monkeypatch)
    comms = wire(root)
    result = InstallCliCommand(project=private_directory, repos=GivenRepos(())).apply(comms)
    assert result.config == RegisteredTeamProject(())
    assert wire(root).registry.snapshot().team_project == result.config


def test_an_install_without_flags_keeps_native_team_and_parked_state(
    private_directory, monkeypatch,
):
    import multiprocessing

    from agent_comms.cli_verbs.install import InstallCliCommand
    from agent_comms.storage.store_files import SharedLock
    from test_locked_store import _hold_lock, process

    root = (private_directory / "wire").resolve()
    root.mkdir(mode=0o700)
    comms = wire(root)
    comms.registry.declare(NativeThread("colleague", frozenset(), Checkout(private_directory)))
    comms.registry.declare(NativeThread(
        "parked-teammate", frozenset(), Checkout(private_directory)))
    comms.registry.set_work_state("parked-teammate", ParkedThreadWorkState())
    recorded = RegisteredTeamProject(())
    comms.registry.set_team_project(recorded)
    _harnessless_home(private_directory, monkeypatch)
    before = comms.registry.store.path.read_bytes()
    context = multiprocessing.get_context("spawn")
    locked, release = context.Event(), context.Event()
    # A concurrent reader permits the install's reads, but forbids an unnecessary
    # upgrade to a writer merely to keep the already committed configuration.
    with process(_hold_lock, comms.registry.store.path, SharedLock(), locked, release):
        assert locked.wait(10)
        try:
            result = InstallCliCommand(project=private_directory).apply(comms)
        finally:
            release.set()
    assert comms.registry.store.path.read_bytes() == before
    assert result.config == recorded
    assert wire(root).registry.snapshot().team_project == recorded
    assert comms.registry.require("parked-teammate").work_state == ParkedThreadWorkState()


def test_a_practice_copy_of_a_bus_without_messages_reads_its_history_twice(private_directory, capsys):
    # The laptop's install refused this read: rebuilding the copy's empty index sealed it
    # inside an open transaction, so the commit moved the file past its own seal.
    from agent_comms.cli import CliInvocation

    root = private_directory / "wire"
    root.mkdir(mode=0o700)
    Bus.install(Bus.path_in(root))
    (root / "storage.json").write_text(json.dumps({"floor_gib": 0}))
    practice = PracticeCopy(root, private_directory / 'practice/bus')
    practice.include(tuple(RootFile.existing_in(root)))
    copy = practice.path
    for _ in range(2):
        assert CliInvocation().invoke(["--root", str(copy), "history", "--limit", "50"], sys.stdout) == 0
        assert '"failed"' not in capsys.readouterr().out


def test_practice_copy_takes_turn_files_but_no_live_process(private_directory):
    root = private_directory / "bus"
    Bus.install(Bus.path_in(root))
    comms = wire(root)
    (root / "storage.json").write_text(json.dumps({"floor_gib": 0}))
    comms.registry.declare(NativeThread(
        "owner", frozenset(), Checkout(root), process_identity=ProcessIdentity.capture(os.getpid())))
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
