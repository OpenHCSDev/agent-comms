"""A broken executable must never reach runtime pointer replacement."""

import signal
import subprocess
import sys
from pathlib import Path

import pytest

from agent_comms.active_route import LocalRoute
from agent_comms.owner_cutover import StoppedOwnerInstallation
from tools.publish_runtime import RuntimePublication


@pytest.mark.parametrize("command", ["exit 17", "echo broken >&2; exit 0"])
def test_cli_failure_leaves_links_untouched(tmp_path, monkeypatch, command):
    monkeypatch.setenv("HOME", str(tmp_path))
    runtime = tmp_path / "broken-runtime"
    binary = runtime / "bin/agent-comms"
    binary.parent.mkdir(parents=True)
    binary.write_text("#!/bin/sh\n" + command + "\n")
    binary.chmod(0o700)
    link = tmp_path / "command"
    link.symlink_to("previous-command")
    publication = RuntimePublication(runtime, {link: binary})
    current = StoppedOwnerInstallation.current_pointer()
    current.parent.mkdir(parents=True)
    current.symlink_to("previous-runtime")
    before = (link.lstat(), current.lstat())
    route = LocalRoute(tmp_path / "bus")

    with pytest.raises(RuntimeError, match="Runtime CLI refused publication"):
        publication.publish(route.root, ())

    assert link.readlink() == Path("previous-command")
    assert current.readlink() == Path("previous-runtime")
    assert (link.lstat(), current.lstat()) == before


def test_publication_ends_retired_runtime_processes_and_keeps_the_new_runtimes(
        tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    runtimes = {name: tmp_path / name for name in ("previous-runtime", "runtime")}
    for runtime in runtimes.values():
        (runtime / "bin").mkdir(parents=True)
        (runtime / "bin/python").symlink_to(sys.executable)
    current = StoppedOwnerInstallation.current_pointer()
    current.parent.mkdir(parents=True)
    current.symlink_to(runtimes["previous-runtime"])
    retired, kept = (subprocess.Popen([runtime / "bin/python", "-c", "import time; time.sleep(60)"])
                     for runtime in runtimes.values())
    try:
        ended = RuntimePublication(runtimes["runtime"].resolve(), {}).publish_links(())
        assert [process.identity.pid for process in ended] == [retired.pid]
        assert retired.wait(timeout=10) == -signal.SIGTERM
        assert kept.poll() is None and current.readlink() == runtimes["runtime"].resolve()
        with pytest.raises(ValueError, match="would retire the runtime it publishes"):
            RuntimePublication(runtimes["runtime"].resolve(), {}).publish_links(())
    finally:
        for child in (retired, kept):
            child.kill()
            child.wait()
