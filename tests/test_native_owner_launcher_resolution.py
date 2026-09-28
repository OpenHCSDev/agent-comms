"""The installed owner entrypoint shares the existing native package authority."""

from types import SimpleNamespace

import pytest

from agent_comms import manual_compaction_bridge, native_session_reopen, private_nk_entrypoint
from agent_comms.native_package import NativePackageError
from agent_comms.native_session_reopen import NativeReopenError, package_for_launcher


def launcher(tmp_path, monkeypatch, route):
    executable = tmp_path / "pi-comms-native"
    executable.write_text("#!/bin/sh\nexit 1\n")
    executable.chmod(0o700)
    monkeypatch.setenv("PATH", str(tmp_path))
    monkeypatch.setattr(private_nk_entrypoint, "private_nk_from_environment", lambda: route)
    return executable


def test_installed_owner_and_alias_use_route_and_full_package_verification(tmp_path, monkeypatch):
    package = tmp_path / "package"
    executable = launcher(tmp_path, monkeypatch, SimpleNamespace(native_package=package))
    verified = []
    monkeypatch.setattr(native_session_reopen, "verify_native_package", verified.append)
    assert package_for_launcher("pi-comms-native") == package
    alias = tmp_path / "alias"
    alias.symlink_to(executable)
    assert package_for_launcher(str(alias)) == package
    assert verified == [package, package]


def test_unconfigured_owner_launcher_cannot_fall_back_to_legacy_pi(tmp_path, monkeypatch):
    launcher(tmp_path, monkeypatch, None)
    with pytest.raises(NativeReopenError, match="configured private route"):
        package_for_launcher("pi-comms-native")


def test_route_does_not_override_native_compaction_package_commitment(tmp_path, monkeypatch):
    launcher(tmp_path, monkeypatch, SimpleNamespace(native_package=tmp_path / "wrong"))

    def fail(_package):
        raise NativePackageError("Native package tree differs")

    monkeypatch.setattr(native_session_reopen, "verify_native_package", fail)
    with pytest.raises(NativePackageError, match="tree differs"):
        package_for_launcher("pi-comms-native")


async def test_installed_owner_manual_compaction_cannot_launch_legacy_writer(tmp_path, monkeypatch):
    executable = launcher(tmp_path, monkeypatch, None)
    alias = tmp_path / "alias"
    alias.symlink_to(executable)

    class Owner:
        def __init__(self):
            self.turns = SimpleNamespace(agent_bin=str(alias), turn_locks={}, active_turns={})
            self.turns.sessions = SimpleNamespace(sync_identity=self.sync_identity)

        async def sync_identity(self, session):
            return session

    result = await manual_compaction_bridge.compact_context(Owner().turns, "owner")
    assert result == {
        "ok": False,
        "error": "Canonical native compaction requires the owner journal bridge.",
    }
