"""Later publication cannot broaden an acquired empty Bus snapshot."""
from concurrent.futures import ThreadPoolExecutor

from agent_comms.projects.checkouts import Checkout
from agent_comms.messaging.read_basis import NamedViewer
from agent_comms.registry.threads import NativeThread


def later_counts(comms, viewer):
    with comms.bus.snapshot(viewer=NamedViewer(viewer)) as (basis, source):
        return comms.bus.pending_counts_opened(source, basis)


def test_original_empty_cut_cannot_clear_later_deliveries(comms, tmp_path):
    comms.registry.declare(NativeThread("sender", frozenset(), Checkout(tmp_path)))
    viewer = comms.messaging.user_identity(Checkout(tmp_path)).name
    with comms.bus.snapshot(viewer=NamedViewer(viewer)) as (basis, source):
        assert source.revision() == 0
        message = comms.messaging.send("sender", viewer, "after empty capture").message
        # A nested read in this thread borrows the held snapshot (locked_store.py:993);
        # SQLiteReaders is per thread, so the later cut is taken from another one.
        with ThreadPoolExecutor(max_workers=1) as later:
            assert later.submit(later_counts, comms, viewer).result() == {"sender": 1}
        assert comms.bus.pending_counts_opened(source, basis) == {}
    assert comms.bus.bus.revision() == message.seq
