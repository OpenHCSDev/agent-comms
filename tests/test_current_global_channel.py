import pytest

from agent_comms.channel_targets import BuiltinChannel

from agent_comms.errors import UnregisteredThreadError
from agent_comms.threads import Thread
from agent_comms.comms import Comms
from agent_comms.native_settings import NativeThreadDefaults
from agent_comms.active_route import resolve_comms_route
from agent_comms.launch_runtime import LaunchRuntime


def test_all_is_the_only_global_target(tmp_path):
    comms = Comms(resolve_comms_route(tmp_path), runtime=LaunchRuntime.current())
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
