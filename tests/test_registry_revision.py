"""Registry reads reuse unchanged declarations but follow external wire writes."""

import json
from unittest.mock import patch

import pytest

from agent_comms import Thread
from agent_comms.declarations import ThreadRegistry, ThreadStatus, _atomic_write_text


def test_repeated_reads_parse_once_and_observe_external_changes(tmp_path):
    path = tmp_path / "registry.json"
    writer = ThreadRegistry(path)
    writer.register(Thread(name="worker", tags=frozenset(), worktree=str(tmp_path)))
    reader = ThreadRegistry(path)
    original = reader.require("worker")
    with patch("agent_comms.declarations.json.loads", wraps=json.loads) as loads:
        for _ in range(20):
            assert reader.require("worker") is original
            assert reader.status("worker") is ThreadStatus.RUNNING
            assert reader.all_threads()["worker"] is original
        assert loads.call_count == 0
        writer.unregister("worker")
        assert reader.status("worker") is ThreadStatus.STOPPED
    # External atomic replacement invalidates both identities and status.
    contents = path.read_text().replace('"worker"', '"renamed"')
    _atomic_write_text(path, contents)
    assert "worker" not in reader
    assert reader.require("renamed").name == "renamed"
    path.unlink()
    assert not reader.all_threads()


def test_malformed_replacement_never_serves_stale_success(tmp_path):
    path = tmp_path / "registry.json"
    registry = ThreadRegistry(path)
    registry.register(Thread(name="worker", tags=frozenset(), worktree=str(tmp_path)))
    saved = path.read_text()
    registry.require("worker")
    _atomic_write_text(path, "{")
    for _ in range(2):
        with pytest.raises(json.JSONDecodeError):
            registry.require("worker")
    _atomic_write_text(path, saved)
    assert registry.require("worker").name == "worker"
