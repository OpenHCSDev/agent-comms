"""Enumerate only a test fixture's source to assert exact paint membership."""

def displayed_sequences(comms, viewer, snapshot=None, *, document=None):
    snapshot = comms.registry.snapshot() if snapshot is None else snapshot
    from agent_comms.read_ledger import ReadSource
    with comms.bus.log._opened_wire_snapshot(need_sequence=False) as source:
        mask = comms.bus.reads.membership(viewer, snapshot, ReadSource.from_revision(source.revision), document=document)
        return frozenset(message.seq for record in source.public_records()
                         for message in source.messages(record) if mask.contains(message))
