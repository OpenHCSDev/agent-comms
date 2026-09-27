"""A stopped source preserves its pending and uncertain bytes in an archive."""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys

import pytest

from agent_comms import supervised_cutover
from agent_comms.declarations import RelationViolationError, Thread
from agent_comms.input_disposition import InputDispositions
from agent_comms.operations import Comms
from agent_comms.supervised_cutover import archive_stopped_root

pytestmark = pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux cutover")


def test_archive_refuses_live_owner_then_preserves_pending_and_unknown(tmp_path):
    comms = Comms(tmp_path / "wire")
    process = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        for name in ("sender", "receiver"):
            comms.register(Thread(name, frozenset(), str(tmp_path), pid=process.pid))
        comms.send("sender", "receiver", "not yet read")
        InputDispositions(comms.root).record(
            "test:unknown", seq=None, owner="receiver", admission=1,
            target="receiver", text="uncertain input",
        )
        destination = tmp_path / "private-archive" / "snapshot"
        with pytest.raises(RelationViolationError, match="all old owners stopped"):
            archive_stopped_root(comms, destination)
        for name in ("sender", "receiver"):
            comms.registry.unregister(name)
        with pytest.raises(RelationViolationError, match="all old owners stopped"):
            archive_stopped_root(comms, destination)
        assert not destination.exists()
        process.terminate()
        process.wait(timeout=5)
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)
    receipt = archive_stopped_root(comms, destination)
    manifest = json.loads((destination / ".archive-manifest").read_text())
    assert receipt.pending_messages == manifest["pending_messages"] == 1
    assert receipt.unknown_inputs == manifest["unknown_inputs"] == 1
    for name, evidence in manifest["files"].items():
        assert (destination / name).read_bytes() == (comms.root / name).read_bytes()
        assert hashlib.sha256((destination / name).read_bytes()).hexdigest() == evidence["sha256"]
    assert (destination / "bus.jsonl").stat().st_mode & 0o777 == 0o600
    with pytest.raises(ValueError, match="already exists"):
        archive_stopped_root(comms, destination)


def test_archive_refuses_path_redirected_back_into_source(tmp_path):
    comms = Comms(tmp_path / "wire")
    alias = tmp_path / "source-alias"
    alias.symlink_to(comms.root, target_is_directory=True)
    with pytest.raises(ValueError, match="outside the old root"):
        archive_stopped_root(comms, alias / "snapshot")


def test_archive_refuses_rival_destination_after_staging(tmp_path, monkeypatch):
    comms = Comms(tmp_path / "wire")
    for name in ("sender", "receiver"):
        comms.register(Thread(name, frozenset(), str(tmp_path), pid=0))
    comms.send("sender", "receiver", "pending")
    InputDispositions(comms.root).record(
        "test:unknown", seq=None, owner="receiver", admission=1,
        target="receiver", text="uncertain input",
    )
    for name in ("sender", "receiver"):
        comms.registry.unregister(name)
    destination = tmp_path / "private-archive" / "snapshot"
    original = supervised_cutover._publish_archive_noreplace
    rival_inode: list[int] = []

    def rival_after_copy(stage, target):
        target.mkdir(mode=0o700)
        rival_inode.append(target.stat().st_ino)
        return original(stage, target)

    monkeypatch.setattr(supervised_cutover, "_publish_archive_noreplace", rival_after_copy)
    with pytest.raises(ValueError, match="already exists"):
        archive_stopped_root(comms, destination)
    assert destination.is_dir() and destination.stat().st_ino == rival_inode[0]
    assert list(destination.iterdir()) == []
    assert not list(destination.parent.glob(".cutover-archive-*"))
