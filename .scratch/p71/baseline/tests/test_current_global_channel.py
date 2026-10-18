import pytest

from agent_comms.channel_targets import BuiltinChannel
from agent_comms.comms import wire
from agent_comms.errors import UnregisteredThreadError
from agent_comms.threads import Thread


def test_all_is_the_only_global_target(tmp_path):
    comms = wire(tmp_path)
    for name in ("sender", "first", "second"):
        comms.registry.declare(Thread(name, frozenset(), str(tmp_path)))
    assert BuiltinChannel.lookup("broadcast") is None
    with pytest.raises(UnregisteredThreadError):
        comms.messaging.send("sender", "broadcast", "must not become a global message")
    assert not comms.bus.log.full_history()

    global_message = comms.messaging.send_message("sender", "#all", "global")
    assert global_message.target == "#all"
    assert comms.bus.pending_count("first") == comms.bus.pending_count("second") == 1

    comms.registry.declare(Thread("broadcast", frozenset(), str(tmp_path)))
    direct = comms.messaging.send_message("sender", "broadcast", "ordinary named recipient")
    assert direct.target == "broadcast"
    assert comms.bus.pending_count("first") == comms.bus.pending_count("second") == 1
    # Membership begins at registration; the earlier global message is not replayed.
    assert comms.bus.pending_count("broadcast", "sender") == 1
