"""A broken executable must never reach runtime pointer replacement."""

import subprocess
import sys

from agent_comms.execution.launch_runtime import LaunchRuntime
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


def test_publication_retires_runtime_links_it_no_longer_declares(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("PATH", str(tmp_path / "no-harnesses"))
    runtime = tmp_path / "runtime"
    (runtime / "bin").mkdir(parents=True)
    (runtime / "bin/python").symlink_to(sys.executable)
    (runtime / "bin/agent-comms-team").write_text("")
    RuntimeResolution(core_commit="renamed-script-fixture", packages={}).save(runtime)
    current = LaunchRuntime.current_pointer()
    current.parent.mkdir(parents=True)
    current.symlink_to(runtime)
    published = tmp_path / ".local/bin"
    published.mkdir(parents=True)
    # A console script a previous publication wrote under its old name, and the owner's own link.
    (published / "agent-comms-commander").symlink_to(current / "bin/agent-comms-commander")
    (published / "own-tool").symlink_to("/usr/bin/env")
    declared = published / "agent-comms-team"

    RuntimePublication(runtime.resolve(), {declared: runtime.resolve() / "bin/agent-comms-team"}).publish_links()

    assert sorted(entry.name for entry in published.iterdir()) == ["agent-comms-team", "own-tool"]
    assert declared.readlink() == current / "bin/agent-comms-team"
