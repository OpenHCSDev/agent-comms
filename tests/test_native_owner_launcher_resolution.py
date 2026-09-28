"""Native launcher identity, route and package authority remain independent."""

from types import SimpleNamespace

import pytest

from agent_comms import native_pi, private_nk_entrypoint
from agent_comms.native_package import NativePackageError
from agent_comms.native_pi import NativePiRpcLaunch, NativePiUnavailable


def launcher(tmp_path, monkeypatch, route):
    executable = tmp_path / "bin" / "pi-comms-native"
    executable.parent.mkdir()
    executable.write_text("#!/bin/sh\nexit 1\n")
    executable.chmod(0o700)
    # Model an installed entrypoint belonging to this interpreter, not an
    # arbitrary executable with a privileged-looking basename.
    monkeypatch.setattr(native_pi.sys, "executable", str(executable.with_name("python")))
    monkeypatch.setenv("PATH", str(executable.parent))
    monkeypatch.setattr(private_nk_entrypoint, "private_nk_from_environment", lambda: route)
    return executable


def test_installed_owner_and_alias_use_route_and_full_package_verification(tmp_path, monkeypatch):
    package = tmp_path / "package"
    executable = launcher(tmp_path, monkeypatch, SimpleNamespace(native_package=package))
    verified = []
    monkeypatch.setattr(native_pi, "_trusted_package", verified.append)
    assert NativePiRpcLaunch.package_for_command("pi-comms-native") == package
    alias = tmp_path / "alias"
    alias.symlink_to(executable)
    assert NativePiRpcLaunch.package_for_command(str(alias)) == package
    assert verified == [package, package]


def test_unconfigured_owner_launcher_cannot_fall_back_to_legacy_pi(tmp_path, monkeypatch):
    launcher(tmp_path, monkeypatch, None)
    with pytest.raises(NativePiUnavailable, match="configured pinned package"):
        NativePiRpcLaunch.package_for_command("pi-comms-native")


def test_route_does_not_override_native_package_commitment(tmp_path, monkeypatch):
    launcher(tmp_path, monkeypatch, SimpleNamespace(native_package=tmp_path / "wrong"))

    def fail(_package):
        raise NativePackageError("Native package tree differs")

    monkeypatch.setattr(native_pi, "_trusted_package", fail)
    with pytest.raises(NativePackageError, match="tree differs"):
        NativePiRpcLaunch.package_for_command("pi-comms-native")


def test_direct_pinned_cli_uses_same_package_authority(tmp_path, monkeypatch):
    package = tmp_path / "package"
    cli = package / "dist" / "cli.js"
    cli.parent.mkdir(parents=True)
    cli.touch()
    monkeypatch.setattr(
        private_nk_entrypoint,
        "private_nk_from_environment",
        lambda: SimpleNamespace(native_package=package),
    )
    verified = []
    monkeypatch.setattr(native_pi, "_trusted_package", verified.append)
    assert NativePiRpcLaunch.package_for_command(str(cli)) == package
    assert verified == [package]
