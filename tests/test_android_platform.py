"""Android is a declared platform, not a rewritten sys.platform value."""

import os
import stat
import sys

from agent_comms.execution.child_process import Platform


def test_android_selects_its_file_and_process_operations(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "platform", "android")
    platform = Platform.current()
    process = platform.identity(os.getpid())
    assert process.pid == os.getpid()
    assert platform.arguments(process)
    path = tmp_path / "private"
    path.write_text("retained")
    with path.open("rb") as stream:
        platform.make_private_descriptor(stream.fileno(), 0o600)
    platform.sync_file(path)
    platform.sync_directory(tmp_path)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert path.read_text() == "retained"
