"""Explicit production ACP/worker N/K flags: no inferred activation or provider call."""

from __future__ import annotations

import json
import os
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from agent_comms import (
    acp,
    active_route,
    cli,
    cohort_foreground,
    private_nk_entrypoint,
    worker,
)
from agent_comms.bus_publication import stable_thread_lookup
from agent_comms.child_process import ParentedProcess, ProcessIdentity
from agent_comms.comms import Comms, wire
from agent_comms.coordination_cohort import accept_delivery_cohort
from agent_comms.coordination_errors import IdentityConflict, PublicationActivationBlocked
from agent_comms.coordinator import Coordination
from agent_comms.private_nk_entrypoint import PACKAGE_ENV, ROOT_ID_ENV, PrivateNkLaunch
from agent_comms.threads import Thread
from test_native_prompt_binding import _root
from test_native_prompt_binding import tmp_path as private_root_fixture

tmp_path = private_root_fixture


def test_explicit_owner_entrypoint_requires_exact_root_and_package(tmp_path, monkeypatch):
    root, root_id, _, _, _ = _root(tmp_path)
    monkeypatch.setattr(cohort_foreground, "_trusted_package", lambda _: None)
    assert PrivateNkLaunch.from_environment(root, {}) is None  # marker alone never enables a worker
    for values in (
        {ROOT_ID_ENV: root_id},
        {PACKAGE_ENV: str(tmp_path)},
        {ROOT_ID_ENV: "f" * 32, PACKAGE_ENV: str(tmp_path)},
        {ROOT_ID_ENV: root_id, PACKAGE_ENV: "relative-package"},
        {ROOT_ID_ENV: root_id, PACKAGE_ENV: str(tmp_path), "PI_PROMPT": "legacy"},
    ):
        with pytest.raises((PublicationActivationBlocked, IdentityConflict, ValueError)):
            launch = PrivateNkLaunch.from_environment(root, values)
            if launch is not None:
                launch.validate()
    exact = PrivateNkLaunch.from_environment(root, {ROOT_ID_ENV: root_id, PACKAGE_ENV: str(tmp_path)})
    assert exact is not None
    exact.validate()
    assert exact.wire_root_id == root_id and exact.native_package == tmp_path
    assert exact.validated_root == root
    assert exact.selected_tool_intent is None
    assert not (root / "native-sessions").exists()


def test_private_claim_root_keeps_normal_coding_tools(tmp_path, monkeypatch):

    root = tmp_path / "claim-wire"
    root.mkdir(mode=0o700)
    comms = Comms(root)
    root_id = comms.messaging.initialize_private_initial_protocol()
    monkeypatch.setattr(cohort_foreground, "_trusted_package", lambda _: None)
    selected = PrivateNkLaunch.from_environment(root, {ROOT_ID_ENV: root_id, PACKAGE_ENV: str(tmp_path)})
    assert selected is not None
    assert selected.selected_tool_intent is None


async def test_worker_rejects_partial_private_configuration_before_wire(tmp_path, monkeypatch):
    called = False

    def wire_would_create_root():
        nonlocal called
        called = True
        raise AssertionError("wire should not be constructed")

    monkeypatch.setattr(worker, "wire", wire_would_create_root)
    monkeypatch.setenv("AGENT_COMMS_ROOT", str(tmp_path / "absent"))
    monkeypatch.setenv(ROOT_ID_ENV, "f" * 32)
    monkeypatch.delenv(PACKAGE_ENV, raising=False)
    with pytest.raises(PublicationActivationBlocked, match="exact root ID"):
        await worker.run()
    assert not called and not (tmp_path / "absent").exists()


def test_stdio_acp_refuses_partial_private_configuration_before_wire(tmp_path, monkeypatch):
    called = False

    def wire_would_create_root():
        nonlocal called
        called = True
        raise AssertionError("wire should not be constructed")

    monkeypatch.setattr(acp, "wire", wire_would_create_root)
    monkeypatch.setenv("AGENT_COMMS_ROOT", str(tmp_path / "absent"))
    monkeypatch.setenv(ROOT_ID_ENV, "f" * 32)
    monkeypatch.delenv(PACKAGE_ENV, raising=False)
    monkeypatch.setattr(sys, "argv", ["agent_comms.acp"])
    with pytest.raises(PublicationActivationBlocked, match="exact root ID"):
        acp.main()
    assert not called and not (tmp_path / "absent").exists()


def test_environment_launch_uses_selected_root_not_cwd(tmp_path, monkeypatch):
    root, root_id, _, _, _ = _root(tmp_path)
    seen: list[Path] = []

    def trusted(package: Path):
        seen.append(package)

    monkeypatch.setattr(cohort_foreground, "_trusted_package", trusted)
    monkeypatch.setenv("AGENT_COMMS_ROOT", str(root))
    monkeypatch.setenv(ROOT_ID_ENV, root_id)
    monkeypatch.setenv(PACKAGE_ENV, str(tmp_path))
    monkeypatch.chdir("/")
    selected = private_nk_entrypoint.private_nk_from_environment()
    assert selected is not None and selected.wire_root_id == root_id
    assert selected.native_package == tmp_path and seen == [tmp_path]
    assert selected.validated_root == root
    assert os.environ["AGENT_COMMS_ROOT"] == str(root)


def test_thread_observation_does_not_admit_an_untrusted_native_owner(tmp_path, monkeypatch):
    root, root_id, comms, _, _ = _root(tmp_path)
    monkeypatch.setenv("AGENT_COMMS_ROOT", str(root))
    monkeypatch.setenv(ROOT_ID_ENV, root_id)
    monkeypatch.setenv(PACKAGE_ENV, str(tmp_path))

    def rejected_package(_package):
        raise PublicationActivationBlocked("Reviewed package differs")

    monkeypatch.setattr(cohort_foreground, "_trusted_package", rejected_package)
    name = next(iter(comms.registry.all_threads()))
    observed = wire(root)
    assert observed.views.thread_detail(name, include_pending=False)["name"] == name
    with pytest.raises(PublicationActivationBlocked, match="Reviewed package differs"):
        private_nk_entrypoint.private_nk_from_environment()
    with pytest.raises(PublicationActivationBlocked, match="Reviewed package differs"):
        observed.owners.restart_environment({})
    assert not (root / "native-sessions").exists()


def test_owner_installed_route_selects_same_private_root_for_cli_and_acp(tmp_path, monkeypatch):
    root, root_id, _, _, _ = _root(tmp_path)
    route_file = tmp_path / "active-route.json"
    route_file.write_text(
        json.dumps(
            {
                "version": 1,
                "root": str(root),
                "wire_root_id": root_id,
                "native_package": str(tmp_path),
            }
        )
    )
    route_file.chmod(0o600)
    monkeypatch.setattr(active_route, "active_route_path", lambda: route_file)
    monkeypatch.setattr(cohort_foreground, "_trusted_package", lambda _: None)
    for name in ("AGENT_COMMS_ROOT", ROOT_ID_ENV, PACKAGE_ENV):
        monkeypatch.delenv(name, raising=False)
    routed = wire()
    assert routed.root == root
    assert routed.owners._private_nk_launch == PrivateNkLaunch(root, root_id, tmp_path, None)
    selected = private_nk_entrypoint.private_nk_from_environment()
    assert selected is not None
    assert (selected.validated_root, selected.wire_root_id, selected.native_package) == (
        root,
        root_id,
        tmp_path,
    )
    legacy = tmp_path / "explicit-legacy"
    monkeypatch.setenv("AGENT_COMMS_ROOT", str(legacy))
    assert wire().root == legacy
    assert private_nk_entrypoint.private_nk_from_environment() is None


def test_default_route_owner_start_inherits_exact_private_launch_pin(tmp_path, monkeypatch):
    root, root_id, _, _, _ = _root(tmp_path)
    route_file = tmp_path / "route-state" / "active-route.json"
    monkeypatch.setattr(active_route, "active_route_path", lambda: route_file)
    monkeypatch.setattr(cohort_foreground, "_trusted_package", lambda _: None)
    monkeypatch.delenv("AGENT_COMMS_ROOT", raising=False)
    active_route.publish_active_route(active_route.ActiveRoute(root, root_id, tmp_path))
    comms = wire()
    saved = tmp_path / "saved-session.jsonl"
    saved.write_text('{"type":"session"}\n')
    saved.chmod(0o600)
    comms.registry.declare(
        Thread(
            "resumable", frozenset(), str(tmp_path), process_identity=None, session_file=str(saved)
        )
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
        assert captured[0][ROOT_ID_ENV] == root_id
        assert captured[0][PACKAGE_ENV] == str(tmp_path)
    finally:
        for child in launched:
            child.stop_sync()


def test_publish_route_selects_private_root_and_refuses_replacement(tmp_path, monkeypatch):
    root, root_id, _, _, _ = _root(tmp_path)
    directory = tmp_path / "route-state"
    directory.mkdir(mode=0o755)
    route_file = directory / "active-route.json"
    route = active_route.ActiveRoute(root, root_id, tmp_path)
    monkeypatch.setattr(cohort_foreground, "_trusted_package", lambda _: None)
    monkeypatch.setattr(active_route, "active_route_path", lambda: route_file)
    for name in ("AGENT_COMMS_ROOT", ROOT_ID_ENV, PACKAGE_ENV):
        monkeypatch.delenv(name, raising=False)

    assert active_route.read_active_route() is None
    active_route.publish_active_route(route)
    assert directory.stat().st_mode & 0o777 == 0o700
    assert route_file.stat().st_mode & 0o777 == 0o600
    assert active_route.read_active_route() == route
    assert wire().root == root
    assert private_nk_entrypoint.private_nk_from_environment().validated_root == root
    original = route_file.read_bytes()
    with pytest.raises(ValueError, match="already installed"):
        active_route.publish_active_route(route)
    assert route_file.read_bytes() == original


def test_publish_route_refuses_rival_installed_after_absent_check(tmp_path, monkeypatch):
    root, root_id, _, _, _ = _root(tmp_path)
    directory = tmp_path / "route-state"
    directory.mkdir(mode=0o700)
    route_file = directory / "active-route.json"
    route = active_route.ActiveRoute(root, root_id, tmp_path)
    monkeypatch.setattr(cohort_foreground, "_trusted_package", lambda _: None)
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


def test_default_write_guard_orders_old_root_write_before_route_publication(tmp_path, monkeypatch):
    root, root_id, _, _, _ = _root(tmp_path)
    legacy = tmp_path / ".agent-comms"
    legacy.mkdir(mode=0o700)
    route_file = tmp_path / "route-state" / "active-route.json"
    route_file.parent.mkdir(mode=0o755)
    route = active_route.ActiveRoute(root, root_id, tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(active_route, "active_route_path", lambda: route_file)
    monkeypatch.setattr(cohort_foreground, "_trusted_package", lambda _: None)
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


def test_default_cli_send_holds_route_guard_until_bus_append(tmp_path, monkeypatch):
    root, root_id, _, _, _ = _root(tmp_path)
    legacy = Comms(tmp_path / ".agent-comms")
    for name in ("sender", "receiver"):
        legacy.registry.declare(
            Thread(
                name,
                frozenset(),
                str(tmp_path),
                process_identity=ProcessIdentity.capture(os.getpid()),
            )
        )
    route_file = tmp_path / "route-state" / "active-route.json"
    route = active_route.ActiveRoute(root, root_id, tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("AGENT_COMMS_ROOT", raising=False)
    monkeypatch.setattr(active_route, "active_route_path", lambda: route_file)
    monkeypatch.setattr(cohort_foreground, "_trusted_package", lambda _: None)
    monkeypatch.setattr(cli, "wire", lambda _root=None: legacy)
    monkeypatch.setattr(cli, "_emit", lambda _payload: None)
    entered_send = threading.Event()
    release_send = threading.Event()
    exclusive_requested = threading.Event()
    original_send = legacy.messaging.send
    original_flock = active_route.fcntl.flock

    def delayed_send(*args):
        entered_send.set()
        assert release_send.wait(timeout=5)
        return original_send(*args)

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
        private_nk_entrypoint.private_nk_from_environment()
    assert cli.main(["threads"]) == 1
    assert "invalid JSON" in json.loads(capsys.readouterr().out)["error"]


def test_route_publication_reports_unknown_after_link(tmp_path, monkeypatch):
    root = tmp_path / "private"
    root.mkdir(mode=0o700)
    path = tmp_path / "route-state" / "active-route.json"
    route = active_route.ActiveRoute(root, "a" * 32, tmp_path)
    monkeypatch.setattr(cohort_foreground, "_preflight", lambda *_: None)
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
    monkeypatch.setenv(ROOT_ID_ENV, root_id)
    monkeypatch.setenv(PACKAGE_ENV, str(tmp_path))
    monkeypatch.delenv("PI_PROMPT", raising=False)
    monkeypatch.setattr(sys, "argv", ["agent_comms.acp"])
    seen = []

    def trust_and_switch(_package):
        monkeypatch.chdir(other)

    class StopBeforeOwnerAttachmentError(Exception):
        pass

    def capture_wire(pinned_root):
        seen.append(pinned_root)
        assert pinned_root == root
        assert wire(pinned_root).root == root
        raise StopBeforeOwnerAttachmentError

    monkeypatch.setattr(cohort_foreground, "_trusted_package", trust_and_switch)
    selected = acp if entrypoint == "acp" else worker
    monkeypatch.setattr(selected, "wire", capture_wire)
    with pytest.raises(StopBeforeOwnerAttachmentError):
        if entrypoint == "acp":
            acp.main()
        else:
            await worker.run()
    assert seen == [root]
    assert not (other / "wire").exists()


def test_public_absolute_symlink_handoff_keeps_canonical_root(tmp_path, monkeypatch):
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
        comms.owners._launch_owner_unlocked(Thread("owner", frozenset(), str(tmp_path)), "pi")
    assert seen == [str(physical)]
    assert not (other / "registry.json").exists()


def test_explicit_private_worker_handoff_preserves_pinned_root_and_pair(tmp_path, monkeypatch):
    root, root_id, _, _, _ = _root(tmp_path)
    monkeypatch.setattr(cohort_foreground, "_trusted_package", lambda _: None)
    comms = Comms(root)
    comms.owners.pin_private_nk_launch(root, root_id, tmp_path)
    other = tmp_path / "other"
    other.mkdir()
    monkeypatch.setenv("AGENT_COMMS_ROOT", str(other))
    monkeypatch.setenv(ROOT_ID_ENV, "f" * 32)
    monkeypatch.setenv(PACKAGE_ENV, str(other))
    seen = []

    class StopBeforeSpawnError(Exception):
        pass

    def intercept(_argv, *, env, **_kwargs):
        seen.append((env["AGENT_COMMS_ROOT"], env[ROOT_ID_ENV], env[PACKAGE_ENV]))
        raise StopBeforeSpawnError

    monkeypatch.setattr(ParentedProcess, "launch", intercept)
    with pytest.raises(StopBeforeSpawnError):
        comms.owners._launch_owner_unlocked(Thread("owner", frozenset(), str(tmp_path)), "pi")
    assert seen == [(str(root), root_id, str(tmp_path))]
    assert not (other / "registry.json").exists()


def test_late_private_owner_can_accept_first_message_before_worker_spawn(tmp_path, monkeypatch):
    root, root_id, comms, _, _ = _root(tmp_path)
    monkeypatch.setattr(cohort_foreground, "_trusted_package", lambda _: None)
    comms.owners.pin_private_nk_launch(root, root_id, tmp_path)
    comms.registry.declare(Thread("late-owner", frozenset(), str(tmp_path)))
    late = comms.registry.require("late-owner")
    lookup = stable_thread_lookup(late.created_at)
    message = comms.messaging.send_initial_cohort("sender", "late-owner", "fresh private task")
    with Coordination(str(root / "coordination.sqlite3")) as store:
        participant = store.participants.get(lookup)
        assert participant.committed and participant.owner_thread == "late-owner"

    class StopBeforeSpawnError(Exception):
        pass

    def intercept(_argv, *, env, **_kwargs):
        assert env["AGENT_COMMS_ROOT"] == str(root)
        assert env["AGENT_COMMS_AGENT_BIN"] == str(
            Path(sys.executable).with_name("pi-comms-native")
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
