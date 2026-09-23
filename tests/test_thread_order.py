"""Sort timestamps must reflect thread events, never attachment or heartbeat."""

import json
from datetime import datetime

from agent_comms import Activity, ActivityState, Message, MessageType, Thread, wire


def test_sort_metadata_survives_selection_heartbeat_and_rename(tmp_path, monkeypatch):
    comms = wire(tmp_path)
    comms.register(Thread(name="alpha", tags=frozenset(), worktree=str(tmp_path), created_at=100))
    comms.register(Thread(name="beta", tags=frozenset(), worktree=str(tmp_path), created_at=200))
    comms.activity.emit(Activity(thread="alpha", state=ActivityState.WORKING, timestamp=300))
    comms.bus.send(
        Message(
            sender="alpha", target="beta", body="outgoing", type=MessageType.INFO, timestamp=400
        )
    )
    comms.bus.send(
        Message(
            sender="beta", target="alpha", body="incoming", type=MessageType.INFO, timestamp=500
        )
    )
    assert comms.last_sent_timestamps() == {"alpha": 400, "beta": 500}
    comms.acquire_thread("alpha", owner_pid=0)
    comms.heartbeat("alpha")
    comms.register(Thread(name="alpha", tags=frozenset(), worktree=str(tmp_path)))
    people = {person["name"]: person for person in comms.presence()}
    assert people["alpha"]["created_at"] == 100
    assert people["alpha"]["last_activity"] == 300
    assert people["beta"]["last_activity"] == 0
    assert comms.activity_of("alpha").timestamp == 300
    monkeypatch.setenv("PI_AGENT_ID", "alpha")
    comms.rename_self("renamed")
    assert comms.registry.require("renamed").created_at == 100
    assert comms.last_sent_timestamps() == {"renamed": 400, "beta": 500}
    assert wire(tmp_path).registry.require("alpha").created_at == 100


def test_legacy_creation_uses_session_header_not_last_seen(tmp_path):
    session = tmp_path / "session.jsonl"
    stamp = "2026-01-02T03:04:05Z"
    session.write_text(json.dumps({"type": "session", "timestamp": stamp}) + "\n")
    (tmp_path / "registry.json").write_text(
        json.dumps(
            {
                "threads": {
                    "saved": {
                        "worktree": str(tmp_path),
                        "session_file": str(session),
                        "last_seen": 9999999999,
                    },
                    "unknown": {"worktree": str(tmp_path), "last_seen": 9999999999},
                }
            }
        )
    )
    comms = wire(tmp_path)
    created = datetime.fromisoformat(stamp.replace("Z", "+00:00")).timestamp()
    assert comms.registry.require("saved").created_at == created
    assert comms.registry.require("unknown").created_at == 0
    comms.heartbeat("saved")
    assert wire(tmp_path).registry.require("saved").created_at == created
