"""One physical encoded-row budget, independent of candidate work quantum."""
from __future__ import annotations

import io
import json
from dataclasses import replace

import pytest

from agent_comms.comms import Comms
from agent_comms.errors import RelationViolationError
from agent_comms.messages import InfoKind, Message
from agent_comms.wake_candidate_index import ProjectionUnavailableError, WakeCandidateIndex
from agent_comms.wire_log import CurrentSnapshotOpening, WireLog
from agent_comms.wire_metadata import WireMetadata
from agent_comms.wire_record import WireRecord, WireScan
from bus_history import history


def _row(size: int, newline: bool) -> bytes:
    message = Message('sender', '#team', 'x', InfoKind, seq=1, timestamp=1.0)
    suffix = b'\n' if newline else b''
    overhead = len(json.dumps(message.to_wire()).encode()) + len(suffix) - 1
    message = replace(message, body='x' * (size - overhead))
    raw = json.dumps(message.to_wire()).encode() + suffix
    assert len(raw) == size
    return raw


@pytest.mark.parametrize('extra', [0, 1])
@pytest.mark.parametrize('newline', [True, False])
def test_scan_and_candidate_share_physical_boundary(tmp_path, extra, newline):
    assert WireRecord.max_row_bytes == 8 * 1024 * 1024
    raw = _row(WireRecord.max_row_bytes + extra, newline)
    # A retained public entry: valid before private claim_scope, not a live claim.
    metadata = WireMetadata(
        last_seq=1, claimed_after_seq=1, writer_protocol_version=1,
        wire_root_id='0' * 32,
    )
    scanned = lambda: [WireScan(metadata).read(row) for _, row in WireRecord.rows(io.BytesIO(raw))]
    if extra:
        with pytest.raises(RelationViolationError, match='Oversized'):
            scanned()
    elif not newline:
        with pytest.raises(RelationViolationError, match='Incomplete'):
            scanned()
    else:
        assert scanned()[0].messages()[0].seq == 1

    path = tmp_path / 'row.jsonl'
    path.write_bytes(raw)
    path.chmod(0o600)
    log = WireLog(path)
    log.write_metadata_unlocked(metadata)
    with log.opened_wire_snapshot(opening=CurrentSnapshotOpening()) as source:
        if extra or not newline:
            with pytest.raises(RelationViolationError):
                WakeCandidateIndex._replay_prefix(source, 0, 0, max_rows=1, max_bytes=1)
        else:
            # A legal first row must advance even with a one-byte work quantum.
            batch = WakeCandidateIndex._replay_prefix(source, 0, 0, max_rows=1, max_bytes=1)
            assert batch.next_offset == len(raw)
            assert batch.last_seq == 1
            assert batch.recipients == ()


def test_oversized_append_preserves_marker_and_sequence(tmp_path):
    comms = Comms(tmp_path / 'wire')
    comms.messaging.initialize_private_initial_protocol()
    log = comms.bus.log
    before = log.path.read_bytes(), log.metadata_path.read_bytes()
    row = {'oversized': ''}
    overhead = len(json.dumps(row).encode()) + 1
    row['oversized'] = 'x' * (WireRecord.max_row_bytes - overhead + 1)
    assert len(json.dumps(row).encode()) + 1 == WireRecord.max_row_bytes + 1
    with log.locked():
        metadata = log.read_metadata_unlocked(required=True)
        previous = metadata.last_seq
        with pytest.raises(RelationViolationError, match='Private bus row exceeds the byte limit'):
            log.append_private_unlocked(metadata, row)
        assert metadata.last_seq == previous
    assert (log.path.read_bytes(), log.metadata_path.read_bytes()) == before


READERS = {
    'snapshot rows': lambda log, metadata: log.opened_wire_snapshot(opening=CurrentSnapshotOpening()),
    'snapshot records': lambda log, metadata: log.opened_wire_snapshot(opening=CurrentSnapshotOpening()),
    'verified records': lambda log, metadata: log.locked(),
}
READS = {
    'snapshot rows': lambda source, log, metadata: list(source.rows()),
    'snapshot records': lambda source, log, metadata: list(source.records()),
    'verified records': lambda source, log, metadata: list(log.verified_records_unlocked(metadata)),
}


@pytest.mark.parametrize('bad', ['oversized', 'no newline', 'cut mid-row'])
@pytest.mark.parametrize('reader', READERS)
def test_every_row_reader_refuses_a_bad_physical_row(tmp_path, reader, bad):
    good = _row(200, True)
    raw = {'oversized': _row(WireRecord.max_row_bytes + 1, True),
           'no newline': _row(200, False),
           'cut mid-row': good + good[:100]}[bad]
    metadata = WireMetadata(last_seq=1, claimed_after_seq=1, writer_protocol_version=1, wire_root_id='0' * 32)
    path = tmp_path / 'row.jsonl'
    path.write_bytes(raw)
    path.chmod(0o600)
    log = WireLog(path)
    log.write_metadata_unlocked(metadata)
    with READERS[reader](log, metadata) as source:
        with pytest.raises(RelationViolationError):
            READS[reader](source, log, metadata)


def _message_row(seq: int) -> bytes:
    message = Message('sender', '#team', f'body {seq}', InfoKind, seq=seq, timestamp=1.0)
    return json.dumps(message.to_wire()).encode() + b'\n'


@pytest.mark.parametrize(('reader', 'expected'), [
    ('rows to a stop between rows', 1),
    ('rows to a stop inside the second row', 2),
    ('records to the cut', 3),
])
def test_rows_leave_the_stream_at_the_last_yielded_row(tmp_path, reader, expected):
    rows = [_message_row(seq) for seq in (1, 2, 3)]
    metadata = WireMetadata(last_seq=3, claimed_after_seq=3, writer_protocol_version=1, wire_root_id='0' * 32)
    path = tmp_path / 'rows.jsonl'
    path.write_bytes(b''.join(rows))
    path.chmod(0o600)
    log = WireLog(path)
    log.write_metadata_unlocked(metadata)
    read = {
        'rows to a stop between rows': lambda source: list(source.rows(before_offset=len(rows[0]))),
        'rows to a stop inside the second row': lambda source: list(source.rows(before_offset=len(rows[0]) + 1)),
        'records to the cut': lambda source: list(source.records()),
    }[reader]
    with log.opened_wire_snapshot(opening=CurrentSnapshotOpening()) as source:
        assert len(read(source)) == expected
        assert source.position == len(b''.join(rows[:expected]))


@pytest.mark.parametrize('other', ['Bob', 'sender'])
def test_unknown_segment_only_blocks_its_own_thread(tmp_path, other):
    from conftest import publish_initial
    from agent_comms.errors import StoredHistoryUnreadable
    from agent_comms.field_codec import FieldCodec
    from agent_comms.private_bus_checkpoint import RecordedContextManifestSources
    from agent_comms.turn_context import RecordedContextManifest
    from agent_comms.wire_record import AuthoredObservationWireRecord, RecordedContextManifestWireObservation
    from test_private_bus_checkpoint import _root
    from test_turn_context_wire import manifest

    comms, _ = _root(tmp_path)
    message = publish_initial(comms, 'sender', '#team', '@Alice original question')
    recorded, _ = RecordedContextManifest.capture(manifest(comms.registry.require('Alice')))
    row = AuthoredObservationWireRecord(RecordedContextManifestWireObservation(recorded)).to_wire()
    row['observation']['manifest']['segments'][0]['manifest']['kind'] = 'unavailable_future_segment'
    with comms.bus.log.locked():
        metadata = comms.bus.log.read_metadata_unlocked(required=True)
        comms.bus.log.append_private_unlocked(metadata, row)
    assert history(comms) == [message]
    RecordedContextManifestSources.for_owner(comms.bus.log, other, comms.registry)
    decoded = WireRecord.public_from_wire(row)
    assert FieldCodec.encode(decoded) == row
    with pytest.raises(StoredHistoryUnreadable, match='context sequence.*unavailable_future_segment'):
        RecordedContextManifestSources.for_owner(comms.bus.log, 'Alice', comms.registry)
