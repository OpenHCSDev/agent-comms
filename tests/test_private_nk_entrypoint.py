"""Explicit production ACP/worker N/K flags: no inferred activation or provider call."""

from __future__ import annotations

import json
import os
import subprocess
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
    operations,
    private_nk_entrypoint,
    worker,
)
from agent_comms.coordination_store import IdentityConflict, PublicationActivationBlocked
from agent_comms.declarations import RelationViolationError, Thread
from agent_comms.input_disposition import InputDispositions
from agent_comms.private_nk_entrypoint import PACKAGE_ENV, ROOT_ID_ENV, private_nk_launch
from test_native_prompt_binding import _root
from test_native_prompt_binding import tmp_path as private_root_fixture

tmp_path = private_root_fixture


def test_explicit_owner_entrypoint_requires_exact_root_and_package(tmp_path, monkeypatch):
    root, root_id, _, _, _ = _root(tmp_path)
    monkeypatch.setattr(cohort_foreground, "_trusted_package", lambda _: None)
    assert private_nk_launch(root, {}) is None  # marker alone never enables a worker
    for values in (
        {ROOT_ID_ENV: root_id},
        {PACKAGE_ENV: str(tmp_path)},
        {ROOT_ID_ENV: "f" * 32, PACKAGE_ENV: str(tmp_path)},
        {ROOT_ID_ENV: root_id, PACKAGE_ENV: "relative-package"},
        {ROOT_ID_ENV: root_id, PACKAGE_ENV: str(tmp_path), "PI_PROMPT": "legacy"},
    ):
        with pytest.raises((PublicationActivationBlocked, IdentityConflict, ValueError)):
            private_nk_launch(root, values)
    exact = private_nk_launch(root, {ROOT_ID_ENV: root_id, PACKAGE_ENV: str(tmp_path)})
    assert exact is not None
    assert exact.wire_root_id == root_id and exact.native_package == tmp_path
    assert exact.validated_root == root
    assert not (root / "native-sessions").exists()


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
    assert operations.wire().root == root
    selected = private_nk_entrypoint.private_nk_from_environment()
    assert selected is not None
    assert (selected.validated_root, selected.wire_root_id, selected.native_package) == (
        root,
        root_id,
        tmp_path,
    )
    legacy = tmp_path / "explicit-legacy"
    monkeypatch.setenv("AGENT_COMMS_ROOT", str(legacy))
    assert operations.wire().root == legacy
    assert private_nk_entrypoint.private_nk_from_environment() is None


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
    assert operations.wire().root == root
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


def test_withdraw_route_archives_stopped_private_root(tmp_path, monkeypatch):
    root = tmp_path / "private-wire"
    comms = operations.Comms(root)
    process = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        comms.register(Thread("sender", frozenset(), str(tmp_path), pid=process.pid))
        comms.register(Thread("alpha", frozenset({"team"}), str(tmp_path), pid=process.pid))
        root_id = comms.initialize_private_initial_protocol()
        comms.send_initial_cohort("sender", "#team", "pending private message")
        InputDispositions(root).record(
            "rollback:unknown", seq=None, owner="alpha", admission=1,
            target="alpha", text="uncertain private input",
        )
        route_file = tmp_path / "route-state" / "active-route.json"
        route = active_route.ActiveRoute(root, root_id, tmp_path)
        archive = tmp_path / "archive" / "private-stopped"
        monkeypatch.setattr(cohort_foreground, "_trusted_package", lambda _: None)
        monkeypatch.setattr(active_route, "active_route_path", lambda: route_file)
        active_route.publish_active_route(route)
        with pytest.raises(ValueError, match="expected private root"):
            active_route.withdraw_active_route(
                active_route.ActiveRoute(root, "f" * 32, tmp_path), archive
            )
        with pytest.raises(RelationViolationError, match="all old owners stopped"):
            active_route.withdraw_active_route(route, archive)
        assert route_file.exists() and not archive.exists()
        process.terminate()
        process.wait(timeout=5)
        exclusive_requested = threading.Event()
        original_flock = active_route.fcntl.flock

        def observed_flock(fd, operation):
            if operation == active_route.fcntl.LOCK_EX:
                exclusive_requested.set()
            return original_flock(fd, operation)

        monkeypatch.setattr(active_route.fcntl, "flock", observed_flock)
        with ThreadPoolExecutor(max_workers=1) as executor:
            with active_route.guard_default_route_write(root):
                withdrawing = executor.submit(active_route.withdraw_active_route, route, archive)
                assert exclusive_requested.wait(timeout=5)
                assert route_file.exists() and not archive.exists()
            receipt = withdrawing.result(timeout=5)
        assert not route_file.exists()
        assert receipt.path == archive
        assert (receipt.pending_messages, receipt.unknown_inputs) == (1, 1)
        manifest = json.loads((archive / ".archive-manifest").read_text())
        assert manifest["pending_messages"] == 1
        assert manifest["unknown_inputs"] == 1
        assert (archive / "bus.jsonl").read_bytes() == (root / "bus.jsonl").read_bytes()
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)


def test_invalid_active_route_fails_closed(tmp_path, monkeypatch, capsys):
    route_file = tmp_path / "active-route.json"
    route_file.write_text("{")
    route_file.chmod(0o600)
    monkeypatch.setattr(active_route, "active_route_path", lambda: route_file)
    monkeypatch.delenv("AGENT_COMMS_ROOT", raising=False)
    with pytest.raises(ValueError, match="invalid JSON"):
        operations.wire()
    with pytest.raises(ValueError, match="invalid JSON"):
        private_nk_entrypoint.private_nk_from_environment()
    assert cli.main(["threads"]) == 1
    assert "invalid JSON" in json.loads(capsys.readouterr().out)["error"]


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
        assert operations.wire(pinned_root).root == root
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
    comms = operations.Comms(link)
    seen = []

    class StopBeforeSpawnError(Exception):
        pass

    def intercept(_argv, *, env, **_kwargs):
        seen.append(env["AGENT_COMMS_ROOT"])
        link.unlink()
        link.symlink_to(other, target_is_directory=True)
        # Actual child env continues to select physical A, not retargeted B.
        assert operations.wire(env["AGENT_COMMS_ROOT"]).root == physical
        raise StopBeforeSpawnError

    monkeypatch.setattr(operations.subprocess, "Popen", intercept)
    with pytest.raises(StopBeforeSpawnError):
        comms._launch_owner_unlocked(Thread("owner", frozenset(), str(tmp_path)), "pi")
    assert seen == [str(physical)]
    assert not (other / "registry.json").exists()


def test_explicit_private_worker_handoff_preserves_pinned_root_and_pair(tmp_path, monkeypatch):
    root, root_id, _, _, _ = _root(tmp_path)
    comms = operations.Comms(root)
    comms.pin_private_nk_launch(root, root_id, tmp_path)
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

    monkeypatch.setattr(operations.subprocess, "Popen", intercept)
    with pytest.raises(StopBeforeSpawnError):
        comms._launch_owner_unlocked(Thread("owner", frozenset(), str(tmp_path)), "pi")
    assert seen == [(str(root), root_id, str(tmp_path))]
    assert not (other / "registry.json").exists()
