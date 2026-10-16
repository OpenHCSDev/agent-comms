"""Enumerate only a test fixture's source to assert exact paint membership."""

def displayed_sequences(comms, viewer, snapshot=None, *, document=None):
    snapshot = comms.registry.snapshot() if snapshot is None else snapshot
    mask = comms.bus.reads.membership(viewer, snapshot, document=document)
    with comms.bus.log._record_snapshot(need_sequence=False) as (_, records):
        return frozenset(message.seq for message, _ in records if mask.contains(message))
