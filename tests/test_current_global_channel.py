import pytest

from agent_comms.channel_targets import CommandTarget
from agent_comms.channels import BuiltinChannelView
from agent_comms.comms import wire
from agent_comms.errors import UnregisteredThreadError
from agent_comms.threads import Thread


def test_all_is_the_only_global_target(tmp_path):
    comms = wire(tmp_path)
    for name in ("sender", "first", "second"):
        comms.registry.declare(comms.registry.new_thread(name, frozenset(), str(tmp_path)))
    with pytest.raises(ValueError):
        BuiltinChannelView.decode("broadcast")
    with pytest.raises(UnregisteredThreadError):
        comms.messaging.send("sender", "broadcast", "must not become a global message").message.message_id
    assert not list(comms.bus.full_history_page().messages)

    global_message = comms.messaging.send("sender", "#all", "global").message
    assert global_message.target == "#all"
    assert comms.bus.pending_count("first") == comms.bus.pending_count("second") == 1

    comms.registry.declare(comms.registry.new_thread('broadcast', frozenset(), str(tmp_path)))
    direct = comms.messaging.send("sender", "broadcast", "ordinary named recipient").message
    assert direct.target == "broadcast"
    assert comms.bus.pending_count("first") == comms.bus.pending_count("second") == 1
    # Membership begins at registration; the earlier global message is not replayed.
    assert comms.bus.pending_count("broadcast", CommandTarget.named("sender")) == 1
