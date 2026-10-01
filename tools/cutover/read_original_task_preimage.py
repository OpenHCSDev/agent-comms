"""Read-only original installed writer admission; no target decoder imports."""
import json
from pathlib import Path
import sys

from agent_comms.wire_log import WireLog


def capture_original(bus, source):
    rows = []
    def observed(offset, raw, record):
        originals = record.messages()
        rows.append({'offset': offset, 'raw': raw.decode('utf-8'),
                     'public': originals[0].to_wire() if originals else None})
    tuple(bus.verified_records_unlocked(source.marker, on_row=observed))
    source.require_current()
    return {'root_id': source.marker.root_id, 'through_seq': source.witness.through_seq,
            'rows': rows}


def read_original(root):
    bus = WireLog(Path(root) / 'bus.jsonl')
    with bus.certified_read() as source:
        return capture_original(bus, source)


if __name__ == '__main__':
    original = read_original(sys.argv[1])
    if len(sys.argv) == 3:
        if original['root_id'] != sys.argv[2]:
            raise ValueError('Original task carry root changed before selection.')
        if not any(row['public'] is not None and 'decision' in row['public']
                   for row in original['rows']):
            raise ValueError('No retired authored task requires this carry.')
        print(json.dumps({'root_id': original['root_id'], 'through_seq': original['through_seq']}))
    else:
        print(json.dumps(original, ensure_ascii=False))
