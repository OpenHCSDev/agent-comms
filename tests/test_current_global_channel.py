from agent_comms.storage_rules import StorageRule
import pytest

from agent_comms.channel_targets import BuiltinChannel
from agent_comms.comms import wire
from agent_comms.errors import UnregisteredThreadError
from agent_comms.threads import Thread


def test_all_is_the_only_global_target(tmp_path):
    comms = wire(tmp_path)
    for name in ("sender", "first", "second"):
        comms.registry.declare(Thread.create(name, frozenset(), str(tmp_path), storage_rule=StorageRule()))
    assert BuiltinChannel.lookup("broadcast") is None
    with pytest.raises(UnregisteredThreadError):
        comms.messaging.send("sender", "broadcast", "must not become a global message")
    assert not list(comms.bus.full_history_page().messages)

    global_message = comms.messaging.send_message("sender", "#all", "global")
    assert global_message.target == "#all"
    assert comms.bus.pending_count("first") == comms.bus.pending_count("second") == 1

    comms.registry.declare(Thread.create("broadcast", frozenset(), str(tmp_path), storage_rule=StorageRule()))
    direct = comms.messaging.send_message("sender", "broadcast", "ordinary named recipient")
    assert direct.target == "broadcast"
    assert comms.bus.pending_count("first") == comms.bus.pending_count("second") == 1
    # Membership begins at registration; the earlier global message is not replayed.
    assert comms.bus.pending_count("broadcast", "sender") == 1
