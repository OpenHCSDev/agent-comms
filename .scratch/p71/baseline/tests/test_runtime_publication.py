"""A broken executable must never reach runtime pointer replacement."""

from pathlib import Path

import pytest

from agent_comms.active_route import LocalRoute
from tools.cutover.publish_runtime import RuntimePublication


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
    publication.current.parent.mkdir(parents=True)
    publication.current.symlink_to("previous-runtime")
    before = (link.lstat(), publication.current.lstat())
    route = LocalRoute(tmp_path / "bus")

    with pytest.raises(RuntimeError, match="Runtime CLI refused publication"):
        publication.publish(route.root)

    assert link.readlink() == Path("previous-command")
    assert publication.current.readlink() == Path("previous-runtime")
    assert (link.lstat(), publication.current.lstat()) == before
