"""Native launcher identity, route and package authority remain independent."""
from agent_comms.native_package import pinned_native_package

import pytest

import agent_comms.native_pi as native_pi
import agent_comms.private_nk_entrypoint as private_nk_entrypoint
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
    monkeypatch.setattr(PrivateNkLaunch, "derive", classmethod(lambda cls, root=None: route))
    monkeypatch.setattr(PrivateNkLaunch, "validate", lambda launch: native_pi._trusted_package(launch.native_package))
    return executable


def test_installed_owner_and_alias_use_route_and_full_package_verification(tmp_path, monkeypatch):
    package = tmp_path / "package"
    executable = launcher(tmp_path, monkeypatch, PrivateNkLaunch(tmp_path, "f" * 32, package))
    verified = []
    monkeypatch.setattr(native_pi, "_trusted_package", verified.append)
    assert NativePiRpcLaunch.package_for_command("pi-comms-native", LaunchRuntime.running()) == package
    alias = tmp_path / "alias"
    alias.symlink_to(executable)
    assert NativePiRpcLaunch.package_for_command(str(alias), LaunchRuntime.running()) == package
    assert verified == [package, package]


def test_unconfigured_owner_launcher_cannot_fall_back_to_legacy_pi(tmp_path, monkeypatch):
    launcher(tmp_path, monkeypatch, None)
    with pytest.raises(NativePiUnavailable, match="configured pinned package"):
        NativePiRpcLaunch.package_for_command("pi-comms-native", LaunchRuntime.running())


def test_route_does_not_override_native_package_commitment(tmp_path, monkeypatch):
    launcher(tmp_path, monkeypatch, PrivateNkLaunch(tmp_path, "f" * 32, tmp_path / "wrong"))

    def fail(_package):
        raise NativePackageError("Native package tree differs")

    monkeypatch.setattr(native_pi, "_trusted_package", fail)
    with pytest.raises(NativePackageError, match="tree differs"):
        NativePiRpcLaunch.package_for_command("pi-comms-native", LaunchRuntime.running())


async def test_fork_runs_selected_python_before_copying_saved_history(tmp_path, monkeypatch, *, thread_registration):
    import site
    import venv
    from pathlib import Path

    from agent_comms.comms import Comms
    from agent_comms.fresh_private_session import create_fresh_private_session
    from agent_comms.pi_native_backend import PiNativeBackend
    from agent_comms.threads import Thread

    package = pinned_native_package()
    comms = Comms(tmp_path / "wire")
    comms.messaging.initialize_private_initial_protocol()
    monkeypatch.setenv("AGENT_COMMS_ROOT", str(comms.root))
    selected = tmp_path / "selected-core"
    venv.EnvBuilder(with_pip=False, symlinks=True).create(selected)
    (selected / "bin/pi-comms-native").symlink_to(LaunchRuntime.running().native_entrypoint)
    library = next(selected.glob("lib/python*/site-packages"))
    marker = tmp_path / "selected-python"
    source = Path(__file__).resolve().parents[1] / "src"
    (library / "core-source.pth").write_text("\n".join([
        str(source), *site.getsitepackages(),
        f"import pathlib, sys; pathlib.Path({str(marker)!r}).write_text(sys.prefix)",
    ]) + "\n")
    configuration = tmp_path / "native-config"
    configuration.mkdir(mode=0o700)
    monkeypatch.setenv("PI_CODING_AGENT_DIR", str(configuration))
    monkeypatch.setenv("AGENT_COMMS_NATIVE_CONFIG_DIR", str(configuration))
    parent = create_fresh_private_session(tmp_path / "sessions", worktree=tmp_path)
    original = parent.path.read_bytes()
    copied = await PiNativeBackend.fork(
        thread_registration.new_thread('parent', tags=frozenset(), worktree=str(tmp_path), session_file=parent.session_file),
        str(package / "dist/cli.js"), LaunchRuntime(selected / "bin/python"),
    )
    assert marker.read_text() == str(selected)
    assert Path(copied.path).is_file() and copied.path != parent.session_file
    assert parent.path.read_bytes() == original
