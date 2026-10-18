"""Native launcher identity, route and package authority remain independent."""

import pytest

from agent_comms import native_pi, private_nk_entrypoint
from agent_comms.native_package import NativePackageError
from agent_comms.launch_runtime import LaunchRuntime
from agent_comms.native_pi import NativePiRpcLaunch, NativePiUnavailable
from agent_comms.private_nk_entrypoint import PrivateNkLaunch


def launcher(tmp_path, monkeypatch, route):
    executable = tmp_path / "bin" / "pi-comms-native"
    executable.parent.mkdir()
    executable.write_text("#!/bin/sh\nexit 1\n")
    executable.chmod(0o700)
    # Model an installed entrypoint belonging to this interpreter, not an
    # arbitrary executable with a privileged-looking basename.
    monkeypatch.setattr(native_pi.sys, "executable", str(executable.with_name("python")))
    monkeypatch.setenv("PATH", str(executable.parent))
    monkeypatch.setattr(PrivateNkLaunch, "current", classmethod(lambda cls: route))
    from agent_comms import cohort_foreground
    monkeypatch.setattr(cohort_foreground, "_preflight", lambda root, root_id, package, enabled: native_pi._trusted_package(package))
    return executable


def test_installed_owner_and_alias_use_route_and_full_package_verification(tmp_path, monkeypatch):
    package = tmp_path / "package"
    executable = launcher(tmp_path, monkeypatch, PrivateNkLaunch(tmp_path, "f" * 32, package, None))
    verified = []
    monkeypatch.setattr(native_pi, "_trusted_package", verified.append)
    assert NativePiRpcLaunch.package_for_command("pi-comms-native", LaunchRuntime.running()) == package
    alias = tmp_path / "alias"
    alias.symlink_to(executable)
    assert NativePiRpcLaunch.package_for_command(str(alias), LaunchRuntime.running()) == package
    assert verified == [package, package]


def test_running_owner_keeps_its_start_launcher_after_runtime_relink(tmp_path, monkeypatch):
    package = tmp_path / "package"
    old = tmp_path / "old-runtime" / "bin" / "pi-comms-native"
    new = tmp_path / "new-runtime" / "bin" / "pi-comms-native"
    for executable in (old, new):
        executable.parent.mkdir(parents=True)
        executable.write_text("#!/bin/sh\nexit 1\n")
        executable.chmod(0o700)
    current = tmp_path / "bin" / "pi-comms-native"
    current.parent.mkdir()
    current.symlink_to(old)
    route = PrivateNkLaunch(tmp_path, "f" * 32, package, None)
    monkeypatch.setattr(PrivateNkLaunch, "current", classmethod(lambda cls: route))
    from agent_comms import cohort_foreground
    monkeypatch.setattr(
        cohort_foreground,
        "_preflight",
        lambda root, root_id, selected, enabled: native_pi._trusted_package(selected),
    )
    monkeypatch.setattr(native_pi.sys, "executable", str(new.with_name("python")))
    from agent_comms.native_arguments import NativeArguments
    monkeypatch.setattr(
        NativeArguments, "owner_launcher", classmethod(lambda cls, runtime: str(old))
    )
    monkeypatch.setattr(native_pi, "_trusted_package", lambda _: None)

    current.unlink()
    current.symlink_to(new)
    assert NativePiRpcLaunch.package_for_command(str(old), LaunchRuntime.running()) == package


def test_unconfigured_owner_launcher_cannot_fall_back_to_legacy_pi(tmp_path, monkeypatch):
    launcher(tmp_path, monkeypatch, None)
    with pytest.raises(NativePiUnavailable, match="configured pinned package"):
        NativePiRpcLaunch.package_for_command("pi-comms-native", LaunchRuntime.running())


def test_route_does_not_override_native_package_commitment(tmp_path, monkeypatch):
    launcher(tmp_path, monkeypatch, PrivateNkLaunch(tmp_path, "f" * 32, tmp_path / "wrong", None))

    def fail(_package):
        raise NativePackageError("Native package tree differs")

    monkeypatch.setattr(native_pi, "_trusted_package", fail)
    with pytest.raises(NativePackageError, match="tree differs"):
        NativePiRpcLaunch.package_for_command("pi-comms-native", LaunchRuntime.running())


def test_direct_pinned_cli_uses_same_package_authority(tmp_path, monkeypatch):
    package = tmp_path / "package"
    cli = package / "dist" / "cli.js"
    cli.parent.mkdir(parents=True)
    cli.touch()
    monkeypatch.setattr(PrivateNkLaunch, "current", classmethod(lambda cls: PrivateNkLaunch(tmp_path, "f" * 32, package, None)))
    from agent_comms import cohort_foreground
    monkeypatch.setattr(cohort_foreground, "_preflight", lambda root, root_id, selected, enabled: native_pi._trusted_package(selected))
    verified = []
    monkeypatch.setattr(native_pi, "_trusted_package", verified.append)
    assert NativePiRpcLaunch.package_for_command(str(cli), LaunchRuntime.running()) == package
    assert verified == [package]
