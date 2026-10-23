"""The installer publishes and undoes its own route, leaving other publishers alone."""


import errno
import fcntl
import json
import os
import subprocess

import pytest

from agent_comms.active_route import (
    ActiveRoute,
    RoutePublicationUnknownError,
    RouteReadersHold,
    NoRouteSelection,
    PublishedRouteSelection,
    read_active_route,
    route_new_bus,
)
from agent_comms.bus import Bus
from agent_comms.comms import wire
from agent_comms.installer.backup import StoreBackup, UpgradeRefused
from agent_comms.installer.commands import InstallCommands
from agent_comms.installer.probes.publication import SelectedRouteProbe
from agent_comms.installer.interpreter_operation import InstalledProgram
from agent_comms.launch_runtime import LaunchRuntime
from agent_comms.recorded_bus import RecordedBusIdentity
from bus_record_fixture import prepare_native_comms
from test_carry_bus_history import OUTGOING_V2, predecessor_history


@pytest.fixture
def route_bus(tmp_path, pinned_native):
    """Publishing a route validates its launch, which acquires the pinned native build."""
    root = tmp_path / "wire"
    prepare_native_comms(wire(root))
    return ActiveRoute(root, RecordedBusIdentity.capture(Bus.path_in(root)))


def old_route(path, route):
    """The retired writer's stored v2 shape; the incoming reader must still refuse it."""
    path.parent.mkdir(mode=0o700, parents=True)
    path.write_text(json.dumps({
        "version": 2, "root": str(route.root),
        "wire_root_id": route.launch().wire_root_id,
    }))
    path.chmod(0o600)
    return path.read_bytes()


@pytest.mark.parametrize("had_route", (False, True))
def test_route_publication_and_rollback_with_no_sql_changes(tmp_path, route_bus, had_route):
    path = tmp_path / "state" / "active-route.json"
    original = old_route(path, route_bus) if had_route else None
    backup = StoreBackup.take(route_bus.root, path, (), tmp_path / "spill")

    selected = PublishedRouteSelection(route_bus.root) if had_route else NoRouteSelection()
    backup.rebind_route(route_bus, selected)
    assert read_active_route(path) == route_bus
    assert not backup.original()
    backup.restore()
    assert backup.original()
    if had_route:
        assert path.read_bytes() == original
    else:
        assert not path.exists()


def test_install_of_another_bus_keeps_the_published_selection(tmp_path, route_bus):
    other_root = tmp_path / "other"
    prepare_native_comms(wire(other_root))
    other = ActiveRoute(other_root, RecordedBusIdentity.capture(Bus.path_in(other_root)))
    path = tmp_path / "state" / "active-route.json"
    route_new_bus(other, path)
    original = path.read_bytes()
    backup = StoreBackup.take(route_bus.root, path, (), tmp_path / "spill")
    backup.rebind_route(route_bus, PublishedRouteSelection(other_root))
    assert read_active_route(path) == other
    assert path.read_bytes() == original
    assert backup.original()


def test_concurrent_publication_before_rebind_is_preserved_on_rollback(tmp_path, route_bus):
    path = tmp_path / "state" / "active-route.json"
    old_route(path, route_bus)
    backup = StoreBackup.take(route_bus.root, path, (), tmp_path / "spill")
    route_new_bus(route_bus, path)
    concurrent = path.read_bytes()
    with pytest.raises(UpgradeRefused, match="route changed"):
        backup.rebind_route(route_bus, PublishedRouteSelection(route_bus.root))
    backup.restore()
    assert path.read_bytes() == concurrent
    assert read_active_route(path) == route_bus


def test_concurrent_publication_after_rebind_is_preserved_on_rollback(tmp_path, route_bus):
    path = tmp_path / "state" / "active-route.json"
    old_route(path, route_bus)
    backup = StoreBackup.take(route_bus.root, path, (), tmp_path / "spill")
    backup.rebind_route(route_bus, PublishedRouteSelection(route_bus.root))

    # A replaced bus permits the existing route owner to publish another selection.
    database = Bus.path_in(route_bus.root)
    replacement = route_bus.root / "replacement.sqlite3"
    Bus.copy_file(database, replacement)
    os.replace(replacement, database)
    other_root = tmp_path / "other"
    prepare_native_comms(wire(other_root))
    other = ActiveRoute(other_root, RecordedBusIdentity.capture(Bus.path_in(other_root)))
    route_new_bus(other, path)
    concurrent = path.read_bytes()
    backup.restore()
    assert path.read_bytes() == concurrent
    assert read_active_route(path) == other


@pytest.mark.parametrize("had_route", (False, True))
def test_unknown_publication_is_still_owned_by_rollback(tmp_path, route_bus, had_route, monkeypatch):
    path = tmp_path / "state" / "active-route.json"
    original = old_route(path, route_bus) if had_route else None
    backup = StoreBackup.take(route_bus.root, path, (), tmp_path / "spill")
    directory = path.parent.stat()
    fsync = os.fsync
    failed = False

    def fail_directory_once(fd):
        nonlocal failed
        current = os.fstat(fd)
        if not failed and (current.st_dev, current.st_ino) == (directory.st_dev, directory.st_ino):
            failed = True
            raise OSError(errno.EIO, "directory durability failed after route publication")
        return fsync(fd)

    monkeypatch.setattr(os, "fsync", fail_directory_once)
    with pytest.raises(RoutePublicationUnknownError):
        selected = PublishedRouteSelection(route_bus.root) if had_route else NoRouteSelection()
        backup.rebind_route(route_bus, selected)
    assert failed
    assert read_active_route(path) == route_bus
    backup.restore()
    assert backup.original()
    if had_route:
        assert path.read_bytes() == original
    else:
        assert not path.exists()


def test_staged_probe_reads_the_published_route(tmp_path):
    runtime = LaunchRuntime.running()
    home = tmp_path / "outgoing-home"
    generated = predecessor_history(tmp_path, revision=OUTGOING_V2)
    root = generated / "destination"
    outgoing = tmp_path / "publisher"
    environment = {
        **runtime.child_environment(runtime.environment(os.environ)), "HOME": str(home),
    }
    # The outgoing tree only writes its old record; the staged reader refuses it.
    writer = """from pathlib import Path
import json,sys
from agent_comms.active_route import ActiveRoute, active_route_path
from agent_comms.wire_log import WireLog
root=Path(sys.argv[1])
route=ActiveRoute(root, WireLog(WireLog.path_in(root)).read_metadata_unlocked().root_id)
path=active_route_path()
path.parent.mkdir(mode=0o700,parents=True)
path.write_text(json.dumps(route.to_record()))
path.chmod(0o600)
"""
    written = subprocess.run(
        (runtime.interpreter, "-P", "-c", writer, root),
        env={**environment, "PYTHONPATH": str(outgoing / "src")},
        cwd=tmp_path, capture_output=True, text=True, timeout=60,
    )
    assert written.returncode == 0, written.stdout + written.stderr
    path = home / ".local/state/agent-comms/active-route.json"
    assert json.loads(path.read_text())["version"] == 2
    with InstallCommands() as commands:
        selected = commands.probe(
            SelectedRouteProbe(), runtime.interpreter, env=environment, cwd=tmp_path,
            program=InstalledProgram(),
        )
        assert selected == NoRouteSelection()
        current = ActiveRoute(root, RecordedBusIdentity.capture(Bus.path_in(root)))
        path.write_text(json.dumps(current.to_record()))
        assert commands.probe(
            SelectedRouteProbe(), runtime.interpreter, env=environment, cwd=tmp_path,
            program=InstalledProgram(),
        ) == PublishedRouteSelection(root)
        root.rename(root.with_name("retired"))
        assert commands.probe(
            SelectedRouteProbe(), runtime.interpreter, env=environment, cwd=tmp_path,
            program=InstalledProgram(),
        ) == NoRouteSelection()
        path.unlink()
        assert commands.probe(
            SelectedRouteProbe(), runtime.interpreter, env=environment, cwd=tmp_path,
            program=InstalledProgram(),
        ) == NoRouteSelection()


def test_failure_after_backup_link_restores_a_single_private_route(tmp_path, route_bus, monkeypatch):
    path = tmp_path / "state" / "active-route.json"
    original = old_route(path, route_bus)
    backup = StoreBackup.take(route_bus.root, path, (), tmp_path / "spill")
    replace = os.replace

    def fail_route_replace(source, destination, **directories):
        if destination == path.name:
            raise OSError(errno.EIO, "replace failed after retaining the old route")
        return replace(source, destination, **directories)

    monkeypatch.setattr(os, "replace", fail_route_replace)
    with pytest.raises(RoutePublicationUnknownError):
        backup.rebind_route(route_bus, PublishedRouteSelection(route_bus.root))
    assert path.read_bytes() == original
    assert path.stat().st_nlink == 2
    assert not backup.original()
    backup.restore()
    assert backup.original()
    assert path.read_bytes() == original
    assert path.stat().st_nlink == 1


def test_route_change_refuses_a_live_reader_instead_of_waiting(tmp_path, route_bus):
    path = tmp_path / "state" / "active-route.json"
    route_new_bus(route_bus, path)
    reader = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        fcntl.flock(reader, fcntl.LOCK_SH)
        with pytest.raises(RouteReadersHold, match=f"PID {os.getpid()}: .*close them"):
            StoreBackup.take(route_bus.root, path, (), tmp_path / "spill")
    finally:
        os.close(reader)
