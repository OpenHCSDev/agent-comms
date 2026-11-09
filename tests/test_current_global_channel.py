import pytest

from agent_comms.messaging.channel_targets import CommandTarget
from agent_comms.errors import UnregisteredThreadError
from agent_comms.projects.checkouts import Checkout
from agent_comms.registry.threads import NativeThread
from conftest import installed_comms


def test_all_is_the_only_global_target(tmp_path):
    comms = installed_comms(tmp_path)
    for name in ("sender", "first", "second"):
        comms.registry.declare(NativeThread(name, frozenset(), Checkout(tmp_path)))
    with pytest.raises(UnregisteredThreadError):
        comms.messaging.send("sender", "broadcast", "must not become a global message")
    assert not list(comms.views.history_page(CommandTarget.named("#any")).messages)

    global_message = comms.messaging.send("sender", "#all", "global").message
    assert global_message.target == "#all"
    assert comms.bus.pending_count("first") == comms.bus.pending_count("second") == 1

    comms.registry.declare(NativeThread('broadcast', frozenset(), Checkout(tmp_path)))
    direct = comms.messaging.send("sender", "broadcast", "ordinary named recipient").message
    assert direct.target == "broadcast"
    assert comms.bus.pending_count("first") == comms.bus.pending_count("second") == 1
    # Membership begins at registration; the earlier global message is not replayed.
    assert comms.bus.pending_count("broadcast", CommandTarget.named("sender")) == 1
