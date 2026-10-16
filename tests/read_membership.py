"""Inspect an isolated fixture's authority; never used by production readers."""
from agent_comms.read_ledger import (
    ReadDocument, ReadHeaderRow, ReadMessageRow, ReadTranscriptRow, ReadSource,
)


def stored_read_state(ledger):
    """Deliberately inspect every test row, including retired identities."""
    with ledger.snapshot() as connection:
        header = ReadHeaderRow.current(connection)
        return ReadDocument(
            {row.key: row.progress for row in ReadMessageRow.select(connection)},
            header.source,
            {row.key: row.through for row in ReadTranscriptRow.select(connection)},
            header.notice,
        )


def install_read_state(ledger, document):
    """Explicit test seeding, not a production replacement API or reader fallback."""
    with ledger.writing() as connection:
        ReadHeaderRow.current(connection)
        ReadMessageRow.delete(connection)
        ReadTranscriptRow.delete(connection)
        ReadHeaderRow(document.source, document.notice).upsert(connection)
        for key, progress in document.messages.items():
            ReadMessageRow(key, progress).upsert(connection)
        for key, position in document.transcripts.items():
            ReadTranscriptRow(key, position).upsert(connection)


def read_mask(comms, viewer, snapshot):
    with comms.bus.log._opened_wire_snapshot(need_sequence=False) as source:
        return comms.bus.reads.membership(
            viewer, snapshot, ReadSource.from_revision(source.revision)
        )


def displayed_sequences(comms, viewer, snapshot=None):
    snapshot = comms.registry.snapshot() if snapshot is None else snapshot
    with comms.bus.log._opened_wire_snapshot(need_sequence=False) as source:
        mask = comms.bus.reads.membership(
            viewer, snapshot, ReadSource.from_revision(source.revision)
        )
        return frozenset(
            message.seq for message, _ in source.public_records() if mask.contains(message)
        )
