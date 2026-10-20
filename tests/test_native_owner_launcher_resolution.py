"""Native launcher identity, route and acquired package remain independent."""
import shlex
import sys
from pathlib import Path


from agent_comms import native_package
from agent_comms.launch_root import LaunchRoot
from agent_comms.launch_runtime import LaunchRuntime
from agent_comms.native_package import VerifiedNativePackage


async def test_fork_runs_selected_python_before_copying_saved_history(tmp_path, monkeypatch, *, thread_registration):
    from agent_comms.comms import Comms
    from agent_comms.fresh_private_session import create_fresh_private_session
    from agent_comms.pi_native_backend import PiNativeBackend

    package = VerifiedNativePackage.acquire(native_package.pinned_native_package())
    comms = Comms(tmp_path / "wire")
    comms.messaging.initialize_private_initial_protocol()
    LaunchRoot.from_path(comms.root).install()
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
