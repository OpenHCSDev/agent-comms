"""Queued restarts are deferred, exact-incarnation and at-most-once."""

from __future__ import annotations

import json
import os
import select
from types import SimpleNamespace

import pytest

from agent_comms import restart_queue as queue
from agent_comms.tools import ToolRequest


def fixture(tmp_path, monkeypatch):
    root = tmp_path / "wire"
    root.mkdir(parents=True)
    owner = SimpleNamespace(
        name="one",
        pid=411,
        created_at=123.5,
        role=SimpleNamespace(executable=True),
        process_alive=True,
        active_turn=object(),
    )

    def snapshot():
        return SimpleNamespace(
            threads={"one": owner},
            statuses={"one": SimpleNamespace(active=True)},
            admission_generations={"one": 9},
        )

    calls = []

    def restart(names, **kwargs):
        calls.append((names, kwargs, os.environ.copy()))
        return (SimpleNamespace(previous_pid=411, pid=512),)

    comms = SimpleNamespace(
        root=root,
        _wire_lock_path=root / "wire",
        registry=SimpleNamespace(
            snapshot=snapshot,
            require=lambda name: owner,
        ),
        owners=SimpleNamespace(
            restart_owners=restart,
            _is_local_participant=lambda *a, **k: True,
        ),
    )
    monkeypatch.setattr(queue.subprocess, "Popen", lambda *a, **k: SimpleNamespace(pid=501))
    monkeypatch.setattr(
        queue,
        "_owner_environment",
        lambda *a: {
            "AGENT_COMMS_THREAD": "one",
            "AGENT_COMMS_AGENT_BIN": "/native/pi",
            "PYTHONPATH": "/original",
            "OWNER_SETTING": "kept",
        },
    )
    return comms, owner, calls


@pytest.mark.skipif(queue.sys.platform != "linux", reason="Linux watcher")
def test_busy_owner_defers_then_restarts_with_exact_original_launch(tmp_path, monkeypatch):
    comms, owner, calls = fixture(tmp_path, monkeypatch)
    record = queue.enqueue(comms, "one")
    assert record["state"] == "pending" and record["incarnation"] == [411, 123.5, 9]
    queue.step(comms)
    assert not calls and queue.status(comms, "one")[0]["state"] == "pending"
    owner.active_turn = None
    queue.step(comms)
    assert len(calls) == 1
    assert calls[0][1]["expected_incarnations"] == {"one": (411, 123.5, 9)}
    assert calls[0][1]["agent_bin"] == "/native/pi"
    assert calls[0][2]["PYTHONPATH"] == "/original"
    assert os.environ.get("PYTHONPATH") != "/original"
    assert queue.status(comms, "one")[0]["state"] == "restarted"
    queue.step(comms)
    assert len(calls) == 1


@pytest.mark.skipif(queue.sys.platform != "linux", reason="Linux watcher")
def test_changed_owner_is_stale_and_uncertain_restart_is_never_retried(tmp_path, monkeypatch):
    comms, owner, calls = fixture(tmp_path, monkeypatch)
    queue.enqueue(comms, "one")
    owner.pid = 412
    owner.active_turn = None
    queue.step(comms)
    assert queue.status(comms, "one")[0]["state"] == "stale"
    assert not calls
    comms, owner, calls = fixture(tmp_path / "other", monkeypatch)
    queue.enqueue(comms, "one")
    owner.active_turn = None

    def uncertain(*a, **k):
        calls.append(True)
        raise RuntimeError("signal may have been sent")

    comms.owners.restart_owners = uncertain
    queue.step(comms)
    assert queue.status(comms, "one")[0]["state"] == "uncertain"
    queue.step(comms)
    assert calls == [True]


@pytest.mark.skipif(queue.sys.platform != "linux", reason="Linux watcher")
@pytest.mark.skipif(queue.sys.platform != "linux", reason="Linux watcher")
def test_inotify_wakes_for_registry_and_queue_changes(tmp_path):
    root = tmp_path / "wire"
    root.mkdir()
    directory = root / queue.DIRECTORY
    directory.mkdir()
    with queue._watch(root, directory) as fd:
        (root / "registry.json").write_text("changed")
        assert select.select([fd], [], [], 1)[0] == [fd]
        os.read(fd, 65536)
        (directory / "next.json").write_text("queued")
        assert select.select([fd], [], [], 1)[0] == [fd]


@pytest.mark.skipif(queue.sys.platform != "linux", reason="Linux watcher")
def test_tool_is_registered_and_queue_record_has_no_secrets(tmp_path, monkeypatch):
    comms, _, _ = fixture(tmp_path, monkeypatch)
    names = {type_.declared_name for type_ in ToolRequest.members_with(ToolRequest)}
    assert {"comms_queue_restart", "comms_restart_queue", "comms_cancel_restart"} <= names
    monkeypatch.setenv("TEST_SECRET", "should-not-persist")
    record = queue.enqueue(comms, "one")
    raw = (comms.root / queue.DIRECTORY / f"{record['id']}.json").read_text()
    assert "should-not-persist" not in raw
    assert json.loads(raw)["state"] == "pending"
    assert queue.cancel(comms, "one")[0]["state"] == "cancelled"
    queue.step(comms)
    assert queue.status(comms, "one")[0]["state"] == "cancelled"
