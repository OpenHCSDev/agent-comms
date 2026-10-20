"""Private N/K launch authority for ACP, worker and CLI owners; no provider call."""

from __future__ import annotations

from agent_comms.native_package import VerifiedNativePackage, pinned_native_package
from conftest import publish_initial
import json
import fcntl
import os
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

import agent_comms.acp as acp
import agent_comms.active_route as active_route
import agent_comms.cli as cli
import agent_comms.private_nk_entrypoint as private_nk_entrypoint
import agent_comms.worker as worker
from agent_comms.thread_identity import RecordedThreadIncarnation
from agent_comms.launch_runtime import LaunchRuntime
from agent_comms.child_process import ParentedProcess, ProcessIdentity
from agent_comms.comms import Comms, wire
from agent_comms.coordination_cohort import accept_delivery_cohort
from agent_comms.coordination_errors import IdentityConflict, PublicationActivationBlocked
from agent_comms.coordinator import Coordination
from agent_comms.private_nk_entrypoint import PrivateNkLaunch, RootLaunch, SharedRootLaunch
from agent_comms.threads import Thread
from test_native_prompt_binding import _root
from test_native_prompt_binding import tmp_path as private_root_fixture

tmp_path = private_root_fixture


def test_publish_route_refuses_rival_installed_after_absent_check(tmp_path, monkeypatch, declared_native_package):
    root, root_id, _, _, _ = _root(tmp_path)
    directory = tmp_path / "route-state"
    directory.mkdir(mode=0o700)
    route_file = directory / "active-route.json"
    route = active_route.ActiveRoute(root, root_id)
    rival = b'{"rival":"preserve these bytes"}\n'
    original_link = os.link

    def rival_before_link(source, target, **kwargs):
        route_file.write_bytes(rival)
        route_file.chmod(0o600)
        return original_link(source, target, **kwargs)

    monkeypatch.setattr(active_route.os, "link", rival_before_link)
    with pytest.raises(ValueError, match="already installed"):
        active_route.publish_active_route(route, route_file)
    assert route_file.read_bytes() == rival
    assert not list(directory.glob(".active-route-*.tmp"))


def test_default_write_guard_orders_old_root_write_before_route_publication(tmp_path, monkeypatch, declared_native_package):
    root, root_id, _, _, _ = _root(tmp_path)
    legacy = tmp_path / ".agent-comms"
    legacy.mkdir(mode=0o700)
    route_file = tmp_path / "route-state" / "active-route.json"
    route_file.parent.mkdir(mode=0o755)
    route = active_route.ActiveRoute(root, root_id)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(active_route, "active_route_path", lambda: route_file)
    exclusive_requested = threading.Event()
    original_flock = active_route.fcntl.flock

    def observed_flock(fd, operation):
        if operation == active_route.fcntl.LOCK_EX:
            exclusive_requested.set()
        return original_flock(fd, operation)

    monkeypatch.setattr(active_route.fcntl, "flock", observed_flock)
    with ThreadPoolExecutor(max_workers=1) as executor:
        with active_route.guard_default_route_write(legacy):
            assert route_file.parent.stat().st_mode & 0o777 == 0o700
            publishing = executor.submit(active_route.publish_active_route, route)
            assert exclusive_requested.wait(timeout=5)
            assert not route_file.exists()
            (legacy / "read-marker").write_text("old write completed")
        publishing.result(timeout=5)
    assert route_file.exists()
    assert (legacy / "read-marker").read_text() == "old write completed"
    with (
        pytest.raises(ValueError, match="route changed before write"),
        active_route.guard_default_route_write(legacy),
    ):
        (legacy / "read-marker").write_text("late write")
    assert (legacy / "read-marker").read_text() == "old write completed"
    with active_route.guard_default_route_write(root):
        (root / "read-marker").write_text("new write completed")
    assert (root / "read-marker").read_text() == "new write completed"


def test_client_cleanup_explicit_published_root_and_cross_thread_retirement(tmp_path, monkeypatch):
    root, root_id, _, _, _ = _root(tmp_path)
    route_file = tmp_path / "route-state" / "active-route.json"
    route_file.parent.mkdir(mode=0o700)
    route = active_route.ActiveRoute(root, root_id)
    route_file.write_text(json.dumps(route.to_record()))
    route_file.chmod(0o600)
    monkeypatch.setattr(active_route, "active_route_path", lambda: route_file)
    client = active_route.resolve_comms_route(root)
    descriptor = os.open(route_file.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        with ThreadPoolExecutor(max_workers=1) as executor:
            ownership = client.hold_client_route()
            executor.submit(ownership.__enter__).result(timeout=5)
            with pytest.raises(BlockingIOError):
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            # Retirement is owned by the resource, regardless of the thread
            # where its compatible nested acquisition ran.
            ownership.__exit__(None, None, None)
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with pytest.raises(BlockingIOError), client.hold_client_route():
                raise AssertionError("Published-root client entered publication")
            independent = active_route.LocalRoute(tmp_path / "independent")
            with independent.hold_client_route():
                pass
    finally:
        os.close(descriptor)


def test_route_publication_reports_unknown_after_link(tmp_path, monkeypatch, declared_native_package):
    root = tmp_path / "private"
    root.mkdir(mode=0o700)
    path = tmp_path / "route-state" / "active-route.json"
    route = active_route.ActiveRoute(root, "a" * 32)
    monkeypatch.setattr(PrivateNkLaunch, "validate", lambda launch: None)
    real_fsync = os.fsync

    def fail_after_link(fd):
        if path.exists() and Path(os.readlink(f"/proc/self/fd/{fd}")) == path.parent:
            raise OSError(5, "injected route directory sync failure")
        return real_fsync(fd)

    monkeypatch.setattr(active_route.os, "fsync", fail_after_link)
    with pytest.raises(active_route.RoutePublicationUnknownError, match="UNKNOWN after link"):
        active_route.publish_active_route(route, path)
    assert active_route.read_active_route(path) == route


def test_active_route_refuses_symlink_and_concurrent_replacement(tmp_path, monkeypatch):
    directory = tmp_path / "private"
    directory.mkdir(mode=0o700)
    target = directory / "target.json"
    target.write_text("{}")
    target.chmod(0o600)
    link = directory / "active-route.json"
    link.symlink_to(target)
    with pytest.raises(OSError):
        active_route.read_active_route(link)
    link.unlink()
    link.write_text("{}")
    link.chmod(0o600)
    replacement = directory / "replacement.json"
    replacement.write_text("{}")
    replacement.chmod(0o600)
    original_read = os.read

    def replace_after_read(fd, count):
        data = original_read(fd, count)
        os.replace(replacement, link)
        return data

    monkeypatch.setattr(active_route.os, "read", replace_after_read)
    with pytest.raises(ValueError, match="changed while reading"):
        active_route.read_active_route(link)


def test_an_old_version_route_is_refused_by_its_version_and_replaced_only_by_the_owner(
        tmp_path, monkeypatch, declared_native_package):
    """The reader names the version it found, the file and the command; publication still refuses;
    replace-route keeps the old file beside the new one and never replaces a readable route."""
    from agent_comms.cli_commands import ReplaceRouteCliCommand
    root, root_id, _, _, _ = _root(tmp_path)
    directory = tmp_path / "route-state"
    directory.mkdir(mode=0o700)
    route_file = directory / "active-route.json"
    monkeypatch.setattr(active_route, "active_route_path", lambda: route_file)
    old = json.dumps({"version": 1, "root": str(root), "wire_root_id": root_id,
                      "native_package": str(tmp_path)}).encode()
    route_file.write_bytes(old)
    route_file.chmod(0o600)
    refusal = (f"active comms route {route_file} is version 1; this installation reads version 2. "
               "Replace it with the bus it should select: agent-comms --root ROOT replace-route")
    with pytest.raises(ValueError) as refused:
        active_route.read_active_route()
    assert str(refused.value) == refusal
    with pytest.raises(ValueError, match="is version 1"):
        active_route.publish_active_route(active_route.ActiveRoute(root, root_id))
    replaced = ReplaceRouteCliCommand().apply(Comms(root))
    assert Path(replaced.backup).read_bytes() == old and Path(replaced.backup).parent == directory
    assert active_route.read_active_route() == active_route.ActiveRoute(root, root_id)
    assert json.loads(route_file.read_text())["version"] == active_route.ActiveRoute.version
    with pytest.raises(ValueError, match="is readable"):
        ReplaceRouteCliCommand().apply(Comms(root))
