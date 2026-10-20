"""Native launcher identity, route and acquired package remain independent."""
import os
import shlex
import sys
from pathlib import Path

import pytest

import agent_comms.native_pi as native_pi
from agent_comms import native_package
from agent_comms.launch_root import LaunchRoot
from agent_comms.launch_runtime import LaunchRuntime
from agent_comms.native_package import NativePackageError, VerifiedNativePackage
from agent_comms.native_pi import NativePiRpcLaunch, NativePiUnavailable
from agent_comms.private_nk_entrypoint import PrivateNkLaunch, RootLaunch, SharedRootLaunch


def launcher(tmp_path, monkeypatch, route):
    LaunchRoot(tmp_path).install()
    executable = tmp_path / "bin/pi-comms-native"
    executable.parent.mkdir()
    executable.write_text("#!/bin/sh\nexit 1\n")
    executable.chmod(0o700)
    runtime = LaunchRuntime(executable.with_name("python"))
    monkeypatch.setattr(LaunchRuntime, "running", classmethod(lambda cls: runtime))
    monkeypatch.setenv("PATH", str(executable.parent))
    monkeypatch.setattr(RootLaunch, "derive", classmethod(lambda cls, root: route or SharedRootLaunch(tmp_path)))
    # These cases isolate command identity from the separately tested root guard.
    monkeypatch.setattr(PrivateNkLaunch, "validate", lambda launch: launch.native_package)
    return executable


def test_installed_owner_and_alias_use_route_and_full_package_verification(tmp_path, monkeypatch, declared_native_package):
    calls = []
    digest = native_package.package_tree_digest

    def counted(path):
        calls.append(path)
        return digest(path)

    monkeypatch.setattr(native_package, "package_tree_digest", counted)
    package = VerifiedNativePackage.acquire(declared_native_package)
    executable = launcher(tmp_path, monkeypatch, PrivateNkLaunch(tmp_path, "f" * 32, package))
    assert NativePiRpcLaunch.package_for_command("pi-comms-native", LaunchRuntime.running()) is package
    alias = tmp_path / "alias"
    alias.symlink_to(executable)
    assert NativePiRpcLaunch.package_for_command(str(alias), LaunchRuntime.running()) is package
    assert calls == [declared_native_package]


def test_unconfigured_owner_launcher_cannot_fall_back_to_legacy_pi(tmp_path, monkeypatch):
    launcher(tmp_path, monkeypatch, None)
    with pytest.raises(NativePiUnavailable, match="configured pinned package"):
        NativePiRpcLaunch.package_for_command("pi-comms-native", LaunchRuntime.running())


def test_route_does_not_override_native_package_commitment(tmp_path, monkeypatch, declared_native_package):
    (declared_native_package / "dist/cli.js").write_text("// altered after declaration\n")
    with pytest.raises(NativePackageError, match="tree differs"):
        package = VerifiedNativePackage.acquire(declared_native_package)
        launcher(tmp_path, monkeypatch, PrivateNkLaunch(tmp_path, "f" * 32, package))
        NativePiRpcLaunch.package_for_command("pi-comms-native", LaunchRuntime.running())


async def test_fork_runs_selected_python_before_copying_saved_history(tmp_path, monkeypatch, *, thread_registration):
    from agent_comms.comms import Comms
    from agent_comms.fresh_private_session import create_fresh_private_session
    from agent_comms.pi_native_backend import PiNativeBackend

    package = VerifiedNativePackage.acquire(native_package.pinned_native_package())
    comms = Comms(tmp_path / "wire")
    comms.messaging.initialize_private_initial_protocol()
    LaunchRoot(comms.root).install()
    selected = tmp_path / "selected-core"
    (selected / "bin").mkdir(parents=True)
    marker = tmp_path / "selected-python"
    interpreter = selected / "bin/python"
    interpreter.write_text(
        "#!/bin/sh\n"
        f"printf %s {shlex.quote(str(selected))} > {shlex.quote(str(marker))}\n"
        "unset PYTHONPATH\n"
        f"exec {shlex.quote(str(Path(sys.executable)))} \"$@\"\n"
    )
    interpreter.chmod(0o700)
    (selected / "bin/pi-comms-native").symlink_to(LaunchRuntime.running().native_entrypoint)
    configuration = tmp_path / "native-config"
    configuration.mkdir(mode=0o700)
    monkeypatch.setenv("PI_CODING_AGENT_DIR", str(configuration))
    monkeypatch.setenv("AGENT_COMMS_NATIVE_CONFIG_DIR", str(configuration))
    parent = create_fresh_private_session(tmp_path / "sessions", worktree=tmp_path)
    original = parent.path.read_bytes()
    copied = await PiNativeBackend.fork(
        thread_registration.new_thread('parent', tags=frozenset(), worktree=str(tmp_path), session_file=parent.session_file),
        str(package.cli), LaunchRuntime(interpreter),
    )
    assert marker.read_text() == str(selected)
    assert Path(copied.path).is_file() and copied.path != parent.session_file
    assert parent.path.read_bytes() == original
