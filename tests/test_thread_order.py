"""Sort timestamps must reflect thread events, never attachment or heartbeat."""


import os

from agent_comms.bus_publication import AgentOrigin
from agent_comms.activity import Activity, ActivityState
from agent_comms.comms import wire
from agent_comms.messages import Message, InfoKind
from agent_comms.threads import Thread
from agent_comms.tool_caller import ThreadCaller


def test_sort_metadata_survives_selection_heartbeat_and_rename(tmp_path, monkeypatch):
    comms = wire(tmp_path)
    comms.registry.declare(comms.registry.new_thread(name='alpha', tags=frozenset(), worktree=str(tmp_path), created_at=100))
    comms.registry.declare(comms.registry.new_thread(name='beta', tags=frozenset(), worktree=str(tmp_path), created_at=200))
    comms.agents.activity.emit(Activity(thread="alpha", state=ActivityState.WORKING, timestamp=300))
    comms.bus.publisher.publish_ordinary(
        Message(
            sender="alpha", target="beta", body="outgoing", type=InfoKind, timestamp=400
        ), origin=AgentOrigin()
    ).message_id
    comms.bus.publisher.publish_ordinary(
        Message(
            sender="beta", target="alpha", body="incoming", type=InfoKind, timestamp=500
        ), origin=AgentOrigin()
    ).message_id
    with comms.bus.opened_activity() as clocks:
        snapshot = comms.registry.snapshot()
        assert comms.views.last_sent_timestamps(
            clocks.sent, registry=snapshot, names=snapshot.threads) == {"alpha": 400, "beta": 500}
    comms.owners.acquire_thread("alpha", owner_pid=os.getpid())
    comms.threads.heartbeat("alpha")
    comms.registry.declare(comms.registry.new_thread(name='alpha', tags=frozenset(), worktree=str(tmp_path)))
    people = {person["name"]: person for person in comms.views.presence()}
    assert people["alpha"]["created_at"] == 100
    assert people["alpha"]["last_activity"] == 300
    assert people["beta"]["last_activity"] == 0
    assert comms.agents.activity_of("alpha", snapshot=comms.registry.snapshot()).timestamp == 300
    monkeypatch.setenv("AGENT_COMMS_THREAD", "alpha")
    comms.threads.rename_self("renamed", caller=ThreadCaller("alpha"))
    assert comms.registry.require("renamed").created_at == 100
    with comms.bus.opened_activity() as clocks:
        snapshot = comms.registry.snapshot()
        assert comms.views.last_sent_timestamps(
            clocks.sent, registry=snapshot, names=snapshot.threads) == {"renamed": 400, "beta": 500}
    assert wire(tmp_path).registry.require("alpha").created_at == 100
