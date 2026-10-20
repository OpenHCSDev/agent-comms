

from agent_comms.routing import MessageRoute


def test_incoming_route_distinguishes_channel_and_direct_delivery():
    assert MessageRoute("user", ("#experiment",)).incoming_scope == "#experiment"
    assert MessageRoute("user", ("worker",)).incoming_scope == "direct message"


