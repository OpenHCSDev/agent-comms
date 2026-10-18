"""One physical encoded-row budget, independent of candidate work quantum."""
from __future__ import annotations

import json
from dataclasses import replace

import pytest

from agent_comms.comms import Comms
from agent_comms.errors import RelationViolationError
from agent_comms.messages import InfoKind, Message
from agent_comms.private_path import FileRevision
from agent_comms.wake_candidate_index import ProjectionUnavailableError, WakeCandidateIndex
from agent_comms.wire_log import OpenedWireSnapshot
from agent_comms.wire_metadata import WireMetadata
from agent_comms.wire_record import WireRecord, WireScan


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
    # A retained public entry: valid before private admission, not a live claim.
    metadata = WireMetadata(
        last_seq=1, admission_after_seq=1, writer_protocol_version=1,
        wire_root_id='0' * 32,
    )
    if extra:
        with pytest.raises(RelationViolationError, match='Oversized'):
            WireScan(metadata).read(raw)
    elif not newline:
        with pytest.raises(RelationViolationError, match='Incomplete'):
            WireScan(metadata).read(raw)
    else:
        assert WireScan(metadata).read(raw).messages()[0].seq == 1

    path = tmp_path / 'row.jsonl'
    path.write_bytes(raw)
    with path.open('rb') as stream:
        source = OpenedWireSnapshot(metadata, 1, stream, FileRevision.from_file(stream))
        if extra or not newline:
            with pytest.raises(ProjectionUnavailableError):
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
            log._append_private_unlocked(metadata, row)
        assert metadata.last_seq == previous
    assert (log.path.read_bytes(), log.metadata_path.read_bytes()) == before
