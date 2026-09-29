"""Sort timestamps must reflect thread events, never attachment or heartbeat."""


import os

from agent_comms.activity import Activity, ActivityState
from agent_comms.comms import wire
from agent_comms.messages import Message, MessageType
from agent_comms.threads import Thread


def test_sort_metadata_survives_selection_heartbeat_and_rename(tmp_path, monkeypatch):
    comms = wire(tmp_path)
    comms.registry.declare(Thread(name="alpha", tags=frozenset(), worktree=str(tmp_path), created_at=100))
    comms.registry.declare(Thread(name="beta", tags=frozenset(), worktree=str(tmp_path), created_at=200))
    comms.agents.activity.emit(Activity(thread="alpha", state=ActivityState.WORKING, timestamp=300))
    comms.bus.publisher.publish_ordinary(
        Message(
            sender="alpha", target="beta", body="outgoing", type=MessageType.INFO, timestamp=400
        )
    ).message_id
    comms.bus.publisher.publish_ordinary(
        Message(
            sender="beta", target="alpha", body="incoming", type=MessageType.INFO, timestamp=500
        )
    ).message_id
    assert comms.views.last_sent_timestamps() == {"alpha": 400, "beta": 500}
    comms.owners.acquire_thread("alpha", owner_pid=os.getpid())
    comms.threads.heartbeat("alpha")
    comms.registry.declare(Thread(name="alpha", tags=frozenset(), worktree=str(tmp_path)))
    people = {person["name"]: person for person in comms.views.presence()}
    assert people["alpha"]["created_at"] == 100
    assert people["alpha"]["last_activity"] == 300
    assert people["beta"]["last_activity"] == 0
    assert comms.agents.activity_of("alpha").timestamp == 300
    monkeypatch.setenv("PI_AGENT_ID", "alpha")
    comms.threads.rename_self("renamed")
    assert comms.registry.require("renamed").created_at == 100
    assert comms.views.last_sent_timestamps() == {"renamed": 400, "beta": 500}
    assert wire(tmp_path).registry.require("alpha").created_at == 100
