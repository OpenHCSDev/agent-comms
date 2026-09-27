"""A stopped source preserves its pending and uncertain bytes in an archive."""

from __future__ import annotations

import hashlib
import json
import os

import pytest

from agent_comms.declarations import RelationViolationError, Thread
from agent_comms.input_disposition import InputDispositions
from agent_comms.operations import Comms
from agent_comms.supervised_cutover import archive_stopped_root


def test_archive_refuses_live_owner_then_preserves_pending_and_unknown(tmp_path):
    comms = Comms(tmp_path / "wire")
    for name in ("sender", "receiver"):
        comms.register(Thread(name, frozenset(), str(tmp_path), pid=os.getpid()))
    comms.send("sender", "receiver", "not yet read")
    InputDispositions(comms.root).record(
        "test:unknown",
        seq=None,
        owner="receiver",
        admission=1,
        target="receiver",
        text="uncertain input",
    )
    destination = tmp_path / "private-archive" / "snapshot"
    with pytest.raises(RelationViolationError, match="all old owners stopped"):
        archive_stopped_root(comms, destination)
    assert not destination.exists()
    for name in ("sender", "receiver"):
        comms.registry.unregister(name)
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
