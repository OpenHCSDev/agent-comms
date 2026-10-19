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


def test_owner_installed_route_selects_same_private_root_for_cli_and_acp(tmp_path, monkeypatch, declared_native_package):
    root, root_id, _, _, _ = _root(tmp_path)
    route_file = tmp_path / "active-route.json"
    route_file.write_text(
        json.dumps(
            {
                "version": 2,
                "root": str(root),
                "wire_root_id": root_id,
            }
        )
    )
    route_file.chmod(0o600)
    monkeypatch.setattr(active_route, "active_route_path", lambda: route_file)
    for name in ("AGENT_COMMS_ROOT",):
        monkeypatch.delenv(name, raising=False)
    routed = wire()
    assert routed.root == root
    pinned = PrivateNkLaunch(root, root_id, VerifiedNativePackage.acquire(declared_native_package))
    assert routed.owners.launch_authority == pinned
    assert RootLaunch.derive(active_route.resolve_comms_route()) == pinned
    legacy = tmp_path / "explicit-legacy"
    monkeypatch.setenv("AGENT_COMMS_ROOT", str(legacy))
    assert wire().root == legacy
    assert RootLaunch.derive(active_route.resolve_comms_route()) == SharedRootLaunch(legacy)


def test_headless_start_derives_launch_from_marker_and_installation(tmp_path, monkeypatch, native_runtime_package):
    """No route and no handoff: the marker owns the root ID, the installation owns the package."""
    from agent_comms.native_package import NativePackageError
    from agent_comms.native_pi import NativePiUnavailable

    root, root_id, _, _, _ = _root(tmp_path)
    package = native_runtime_package.path
    monkeypatch.setattr(active_route, "active_route_path", lambda: tmp_path / "absent" / "active-route.json")
    monkeypatch.setattr(private_nk_entrypoint, "pinned_native_package", lambda: package)
    for name in ("AGENT_COMMS_ROOT",):
        monkeypatch.delenv(name, raising=False)
    shared = wire(tmp_path / "public").owners
    assert shared.launch_authority == SharedRootLaunch(tmp_path / "public")
    with pytest.raises(PublicationActivationBlocked, match="requires a private bus root"):
        shared.require_private_launch()

    comms = wire(root)
    assert comms.owners.launch_authority == PrivateNkLaunch(root, root_id, VerifiedNativePackage.acquire(package))
    saved = tmp_path / "saved-session.jsonl"
    saved.write_text('{"type":"session"}\n')
    saved.chmod(0o600)
    comms.registry.declare(
        comms.registry.new_thread('headless', frozenset(), str(tmp_path), session_file=str(saved))
    )

    def rejected(_package):
        raise NativePiUnavailable("Pinned native Pi package differs from reviewed fork")

    with monkeypatch.context() as package_patch:
        package_patch.setattr(VerifiedNativePackage, "acquire", classmethod(lambda cls, path: rejected(path)))
        with pytest.raises(NativePiUnavailable, match="differs from reviewed fork"):
            wire(root).owners.start("headless")

    def missing():
        raise NativePackageError("Pinned native package directory is missing")

    monkeypatch.setattr(private_nk_entrypoint, "pinned_native_package", missing)
    with pytest.raises(PublicationActivationBlocked, match="prepared native package"):
        wire(root).owners.start("headless")
    assert comms.registry.require("headless").process_identity is None

    monkeypatch.setattr(private_nk_entrypoint, "pinned_native_package", lambda: package)
    launched: list[ParentedProcess] = []
    captured: list[dict[str, str]] = []
    launch = ParentedProcess.launch

    def provider_free_child(_argv, **kwargs):
        captured.append(kwargs["env"])
        child = launch((sys.executable, "-c", "import time; time.sleep(10)"), **kwargs)
        launched.append(child)
        return child

    monkeypatch.setattr(ParentedProcess, "launch", provider_free_child)
    try:
        result = wire(root).owners.start("headless")
        assert result.pid == launched[0].pid
        assert captured[0]["AGENT_COMMS_ROOT"] == str(root)
        # The child derives from its root exactly what the parent derived.
        assert RootLaunch.derive(active_route.resolve_comms_route(captured[0]["AGENT_COMMS_ROOT"])) == PrivateNkLaunch(root, root_id, VerifiedNativePackage.acquire(package))
    finally:
        for child in launched:
            child.stop_sync()


def test_default_route_owner_start_inherits_exact_private_launch_pin(tmp_path, monkeypatch, declared_native_package):
    root, root_id, _, _, _ = _root(tmp_path)
    route_file = tmp_path / "route-state" / "active-route.json"
    monkeypatch.setattr(active_route, "active_route_path", lambda: route_file)
    monkeypatch.delenv("AGENT_COMMS_ROOT", raising=False)
    active_route.publish_active_route(active_route.ActiveRoute(root, root_id))
    comms = wire()
    saved = tmp_path / "saved-session.jsonl"
    saved.write_text('{"type":"session"}\n')
    saved.chmod(0o600)
    comms.registry.declare(
        comms.registry.new_thread('resumable', frozenset(), str(tmp_path), session_file=str(saved))
    )
    launched: list[ParentedProcess] = []
    captured: list[dict[str, str]] = []
    launch = ParentedProcess.launch

    def provider_free_child(_argv, **kwargs):
        captured.append(kwargs["env"])
        child = launch((sys.executable, "-c", "import time; time.sleep(10)"), **kwargs)
        launched.append(child)
        return child

    monkeypatch.setattr(ParentedProcess, "launch", provider_free_child)
    try:
        result = comms.owners.start("resumable")
        assert result.pid == launched[0].pid
        assert comms.registry.require("resumable").process_identity == launched[0].identity
        assert captured[0]["AGENT_COMMS_ROOT"] == str(root)
        # The child derives the same launch from the root it is handed.
        assert RootLaunch.derive(active_route.resolve_comms_route(captured[0]["AGENT_COMMS_ROOT"])) == comms.owners.launch_authority
    finally:
        for child in launched:
            child.stop_sync()


def test_publish_route_selects_private_root_and_refuses_replacement(tmp_path, monkeypatch, declared_native_package):
    root, root_id, _, _, _ = _root(tmp_path)
    directory = tmp_path / "route-state"
    directory.mkdir(mode=0o755)
    route_file = directory / "active-route.json"
    route = active_route.ActiveRoute(root, root_id)
    monkeypatch.setattr(active_route, "active_route_path", lambda: route_file)
    for name in ("AGENT_COMMS_ROOT",):
        monkeypatch.delenv(name, raising=False)

    assert active_route.read_active_route() is None
    active_route.publish_active_route(route)
    assert directory.stat().st_mode & 0o777 == 0o700
    assert route_file.stat().st_mode & 0o777 == 0o600
    assert active_route.read_active_route() == route
    assert wire().root == root
    assert RootLaunch.derive(active_route.resolve_comms_route()).validated_root == root
    original = route_file.read_bytes()
    with pytest.raises(ValueError, match="already installed"):
        active_route.publish_active_route(route)
    assert route_file.read_bytes() == original


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


def test_default_cli_send_holds_route_guard_until_bus_append(tmp_path, monkeypatch, declared_native_package):
    root, root_id, _, _, _ = _root(tmp_path)
    legacy = Comms(tmp_path / ".agent-comms")
    for name in ("sender", "receiver"):
        legacy.registry.declare(
            legacy.registry.new_thread(name, frozenset(), str(tmp_path), process_identity=ProcessIdentity.capture(os.getpid()))
        )
    route_file = tmp_path / "route-state" / "active-route.json"
    route = active_route.ActiveRoute(root, root_id)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("AGENT_COMMS_ROOT", raising=False)
    monkeypatch.setattr(active_route, "active_route_path", lambda: route_file)
    monkeypatch.setattr(cli, "wire", lambda _root=None: legacy)
    entered_send = threading.Event()
    release_send = threading.Event()
    exclusive_requested = threading.Event()
    original_send = legacy.messaging.send
    original_flock = active_route.fcntl.flock

    def delayed_send(*args, **kwargs):
        entered_send.set()
        assert release_send.wait(timeout=5)
        return original_send(*args, **kwargs)

    def observed_flock(fd, operation):
        if operation == active_route.fcntl.LOCK_EX:
            exclusive_requested.set()
        return original_flock(fd, operation)

    monkeypatch.setattr(legacy.messaging, "send", delayed_send)
    monkeypatch.setattr(active_route.fcntl, "flock", observed_flock)
    with ThreadPoolExecutor(max_workers=2) as executor:
        sending = executor.submit(
            cli.main, ["send", "--from", "sender", "--to", "receiver", "--body", "old"]
        )
        assert entered_send.wait(timeout=5)
        publishing = executor.submit(active_route.publish_active_route, route)
        assert exclusive_requested.wait(timeout=5)
        assert not route_file.exists()
        release_send.set()
        assert sending.result(timeout=5) == 0
        publishing.result(timeout=5)
    assert route_file.exists()
    assert [message.body for message in legacy.bus.inbox("receiver")] == ["old"]
    assert cli.main(["send", "--from", "sender", "--to", "receiver", "--body", "late"]) == 1
    assert [message.body for message in legacy.bus.inbox("receiver")] == ["old"]


def test_invalid_active_route_fails_closed(tmp_path, monkeypatch, capsys):
    route_file = tmp_path / "active-route.json"
    route_file.write_text("{")
    route_file.chmod(0o600)
    monkeypatch.setattr(active_route, "active_route_path", lambda: route_file)
    monkeypatch.delenv("AGENT_COMMS_ROOT", raising=False)
    with pytest.raises(ValueError, match="invalid JSON"):
        wire()
    with pytest.raises(ValueError, match="invalid JSON"):
        RootLaunch.derive(active_route.resolve_comms_route())
    assert cli.main(["threads"]) == 1
    assert "invalid JSON" in json.loads(capsys.readouterr().out)["error"]


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


@pytest.mark.parametrize("entrypoint", ["acp", "worker"])
async def test_private_relative_root_pinned_across_cwd_change_before_wire(
    tmp_path, monkeypatch, entrypoint
):
    """Both actual entrypoints must attach A despite a preflight callback moving cwd to B."""
    root, root_id, _, _, _ = _root(tmp_path)
    other = tmp_path / "other"
    other.mkdir()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("AGENT_COMMS_ROOT", "wire")
    monkeypatch.delenv("PI_PROMPT", raising=False)
    monkeypatch.setattr(sys, "argv", ["agent_comms.acp"])
    seen = []

    acquire = VerifiedNativePackage.acquire

    def trust_and_switch(path):
        package = acquire(path)
        monkeypatch.chdir(other)
        return package

    class StopBeforeOwnerAttachmentError(Exception):
        pass

    def capture_wire():
        comms = wire()
        seen.append(comms)
        return comms

    def stop(*_args, **_kwargs):
        raise StopBeforeOwnerAttachmentError

    monkeypatch.setattr(VerifiedNativePackage, "acquire", classmethod(lambda cls, path: trust_and_switch(path)))
    selected = acp if entrypoint == "acp" else worker
    monkeypatch.setattr(selected, "wire", capture_wire)
    if entrypoint == "acp":
        monkeypatch.setattr(acp.asyncio, "run", stop)
    else:
        monkeypatch.setattr(worker.ThreadLaunch, "current", stop)
    with pytest.raises(StopBeforeOwnerAttachmentError):
        if entrypoint == "acp":
            acp.main()
        else:
            await worker.run()
    comms, = seen
    assert comms.root == root
    assert comms.owners.launch_authority.validated_root == root
    assert not (other / "wire").exists()


def test_public_absolute_symlink_handoff_keeps_canonical_root(tmp_path, monkeypatch, *, thread_registration):
    """Private lexical pinning must not regress the existing public child path."""
    if os.name != "posix":
        pytest.skip("symlink handoff control requires POSIX")
    physical = tmp_path / "physical"
    physical.mkdir()
    other = tmp_path / "other"
    other.mkdir()
    link = tmp_path / "public-link"
    link.symlink_to(physical, target_is_directory=True)
    comms = Comms(link)
    seen = []

    class StopBeforeSpawnError(Exception):
        pass

    def intercept(_argv, *, env, **_kwargs):
        seen.append(env["AGENT_COMMS_ROOT"])
        link.unlink()
        link.symlink_to(other, target_is_directory=True)
        # Actual child env continues to select physical A, not retargeted B.
        assert wire(env["AGENT_COMMS_ROOT"]).root == physical
        raise StopBeforeSpawnError

    monkeypatch.setattr(ParentedProcess, "launch", intercept)
    with pytest.raises(StopBeforeSpawnError):
        comms.owners._launch_owner_unlocked(thread_registration.new_thread('owner', frozenset(), str(tmp_path)), "pi", launch_runtime=LaunchRuntime.running())
    assert seen == [str(physical)]
    assert not (other / "registry.json").exists()


def test_late_private_owner_can_accept_first_message_before_worker_spawn(tmp_path, monkeypatch):
    root, root_id, comms, _, _ = _root(tmp_path)
    comms.registry.declare(comms.registry.new_thread('late-owner', frozenset(), str(tmp_path)))
    late = comms.registry.require("late-owner")
    lookup = RecordedThreadIncarnation("fixture", late.created_at).lookup
    message = publish_initial(comms, "sender", "late-owner", "fresh private task")
    with Coordination(str(root / "coordination.sqlite3")) as store:
        participant = store.participants.get(lookup)
        assert participant.committed and participant.owner_thread == "late-owner"

    class StopBeforeSpawnError(Exception):
        pass

    def intercept(_argv, *, env, **_kwargs):
        assert env["AGENT_COMMS_ROOT"] == str(root)
        assert env["AGENT_COMMS_AGENT_BIN"] == str(
            LaunchRuntime.running().native_entrypoint
        )
        with Coordination(str(root / "coordination.sqlite3")) as store:
            participant = store.participants.get(lookup)
            assert participant.committed and participant.owner_thread == "late-owner"
        raise StopBeforeSpawnError

    monkeypatch.setattr(ParentedProcess, "launch", intercept)
    with pytest.raises(StopBeforeSpawnError):
        comms.owners.start("late-owner", agent_bin="pi")
    with Coordination(str(root / "coordination.sqlite3")) as store:
        assert accept_delivery_cohort(comms.bus, root_id, message.seq, store).value.member_count == 1


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
