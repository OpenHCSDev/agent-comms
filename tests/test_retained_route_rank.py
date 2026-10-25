"""Later publication cannot broaden an acquired empty Bus snapshot."""

from agent_comms.repo import Checkout
from agent_comms.read_basis import NamedViewer
from agent_comms.threads import Thread


def test_original_empty_cut_cannot_clear_later_deliveries(comms, tmp_path):
    comms.registry.declare(Thread("sender", frozenset(), Checkout.from_wire(str(tmp_path))))
    viewer = comms.messaging.user_identity(str(tmp_path)).name
    with comms.bus.snapshot(viewer=NamedViewer(viewer)) as (basis, source):
        assert source.revision() == 0
        message = comms.messaging.send("sender", viewer, "after empty capture").message
        with comms.bus.snapshot(viewer=NamedViewer(viewer)) as (later_basis, later_source):
            assert comms.bus.pending_counts_opened(later_source, later_basis) == {"sender": 1}
        assert comms.bus.pending_counts_opened(source, basis) == {}
        assert comms.bus.bus.revision() == message.seq
