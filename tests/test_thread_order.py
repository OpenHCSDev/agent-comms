"""Sort timestamps must reflect thread events, never attachment or heartbeat."""

from agent_comms.comms import wire
from agent_comms.messaging.bus_publication import AgentOrigin
from agent_comms.messaging.channel_targets import CommandTarget
from agent_comms.messaging.messages import InfoKind, Message
from agent_comms.projects.checkouts import Checkout
from agent_comms.registry.activity import Activity, ActivityState
from agent_comms.registry.threads import NativeThread
from agent_comms.transport.tool_caller import ThreadCaller
from conftest import installed_comms


def test_sort_metadata_survives_selection_heartbeat_and_rename(tmp_path):
    comms = installed_comms(tmp_path)
    comms.registry.declare(NativeThread(name='alpha', tags=frozenset(), worktree=Checkout(tmp_path), created_at=100))
    comms.registry.declare(NativeThread(name='beta', tags=frozenset(), worktree=Checkout(tmp_path), created_at=200))
    comms.agents.activity.emit(Activity(thread="alpha", state=ActivityState.WORKING, timestamp=300))
    comms.bus.publisher.publish_ordinary(
        Message(
            sender="alpha", target="beta", body="outgoing", type=InfoKind, timestamp=400
        ), destination=CommandTarget.named("beta"), origin=AgentOrigin()
    )
    comms.bus.publisher.publish_ordinary(
        Message(
            sender="beta", target="alpha", body="incoming", type=InfoKind, timestamp=500
        ), destination=CommandTarget.named("alpha"), origin=AgentOrigin()
    )
    with comms.bus.opened_activity() as clocks:
        assert comms.views.last_sent_timestamps(clocks.sent.items(), registry=comms.registry.snapshot()) == {"alpha": 400, "beta": 500}
    comms.owners.acquire_thread("alpha")
    comms.threads.heartbeat("alpha")
    comms.registry.declare(NativeThread(name='alpha', tags=frozenset(), worktree=Checkout(tmp_path)))
    people = {person.thread.name: person.to_wire() for person in comms.views.presence()}
    assert people["alpha"]["created_at"] == 100
    assert people["alpha"]["last_activity"] == 300
    assert people["beta"]["last_activity"] == 0
    assert comms.agents.activity_of("alpha", snapshot=comms.registry.snapshot()).timestamp == 300
    comms.threads.rename_self("renamed", caller=ThreadCaller("alpha"))
    assert comms.registry.require("renamed").created_at == 100
    with comms.bus.opened_activity() as clocks:
        assert comms.views.last_sent_timestamps(clocks.sent.items(), registry=comms.registry.snapshot()) == {"renamed": 400, "beta": 500}
    assert wire(tmp_path).registry.require("alpha").created_at == 100
