"""A broken executable must never reach runtime pointer replacement."""

import subprocess
import sys


from agent_comms.launch_runtime import LaunchRuntime
from agent_comms.installer.publication import RuntimePublication
from agent_comms.installer.runtime_resolution import RuntimeResolution


def test_pointer_publication_does_not_kill_unselected_runtime_processes(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    # These tiny process-lifetime runtimes contain no packaged harness commands.
    monkeypatch.setenv("PATH", str(tmp_path / "no-harnesses"))
    runtimes = {name: tmp_path / name for name in ("previous-runtime", "runtime")}
    for runtime in runtimes.values():
        (runtime / "bin").mkdir(parents=True)
        (runtime / "bin/python").symlink_to(sys.executable)
        RuntimeResolution(core_commit="process-lifetime-fixture", packages={}).save(runtime)
    current = LaunchRuntime.current_pointer()
    current.parent.mkdir(parents=True)
    current.symlink_to(runtimes["previous-runtime"])
    retired, kept = (
        subprocess.Popen([runtime / "bin/python", "-c", "import time; time.sleep(60)"])
        for runtime in runtimes.values()
    )
    try:
        RuntimePublication(runtimes["runtime"].resolve(), {}).publish_links()
        assert retired.poll() is None
        assert kept.poll() is None and current.readlink() == runtimes["runtime"].resolve()
        RuntimePublication(runtimes["runtime"].resolve(), {}).publish_links()
        assert retired.poll() is None and kept.poll() is None
        assert current.resolve() == runtimes["runtime"].resolve()
    finally:
        for child in (retired, kept):
            child.kill()
            child.wait()
