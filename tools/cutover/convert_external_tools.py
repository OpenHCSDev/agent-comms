"""One-time P502 external-transcript tool-tag conversion (not a dual-format reader).

Run BEFORE carry_external_pages with the fleet stopped and a verified backup.
Dry-run is read-only. --apply changes only external_item.events and
external_draft.events in one SQLite transaction; it refuses mixed/unknown forms.
The old installed Core validates every original event; current Core validates
and encodes every replacement through the declared TranscriptEvent and row field.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
from collections import Counter
from contextlib import closing
from pathlib import Path

from agent_comms.coordination_database import CoordinationStore
from agent_comms.external_transcript import ExternalDraftRow, ExternalItemRow, ExternalTranscript
from agent_comms.field_codec import FieldCodec
from agent_comms.transcript_events import TranscriptEvent

OLD_PYTHON = Path(
    '/home/ts/.local/share/agent-comms/runtimes/'
    'toad-d71e6ae0c-core-8a9062d5c-native-146049da9201abca-20261018T170408Z/bin/python'
)
OLD_PROOF = '''\
import json, sqlite3, sys
from pathlib import Path
from agent_comms.field_codec import FieldCodec
from agent_comms.transcript_events import TranscriptEvent
conn = sqlite3.connect(Path(sys.argv[1]).resolve().as_uri() + '?mode=ro', uri=True)
counts = {}
for table in ('external_item', 'external_draft'):
    count = 0
    for rowid, text in conn.execute(f'SELECT rowid, events FROM "{table}" ORDER BY rowid'):
        raw = json.loads(text)
        if FieldCodec.encode(FieldCodec.decode(tuple[TranscriptEvent, ...], raw)) != raw:
            raise ValueError(f'old Core changed {table} row {rowid}')
        count += 1
    counts[table] = count
print(json.dumps(counts))
'''


def _events(raw: object, table: str, rowid: int) -> tuple[list[dict], tuple[int, ...], int]:
    if not isinstance(raw, list) or any(not isinstance(event, dict) for event in raw):
        raise ValueError(f'Unrecognized {table} events at row {rowid}')
    moved = []
    new = 0
    for position, event in enumerate(raw):
        tool = event.get('tool')
        if tool is None:
            continue
        if event.get('kind') != 'tool_start' or not isinstance(tool, dict):
            raise ValueError(f'Unrecognized {table} tool at row {rowid}')
        if 'kind' in tool and 'tool' not in tool:
            tool['tool'] = tool.pop('kind')
            moved.append(position)
        elif 'tool' in tool and 'kind' not in tool:
            new += 1
        else:
            raise ValueError(f'Unrecognized {table} tool tag at row {rowid}')
    return raw, tuple(moved), new


def convert(path: Path, *, apply: bool, old_python: Path = OLD_PYTHON) -> dict:
    path = path.resolve(strict=True)
    if not old_python.is_file():
        raise ValueError(f'Old Core decoder is missing: {old_python}')
    fields = {
        'external_item': next(f for f in ExternalItemRow._fields() if f.name == 'events'),
        'external_draft': next(f for f in ExternalDraftRow._fields() if f.name == 'events'),
    }
    counts: Counter[str] = Counter()
    changes: dict[str, list[tuple[str, int]]] = {name: [] for name in fields}
    identities = hashlib.sha256()
    connection = (CoordinationStore.writer(path) if apply
                  else CoordinationStore.read_snapshot(path))
    with closing(connection) as db:
        if apply:
            db.execute('BEGIN IMMEDIATE')
        try:
            for table, declared in fields.items():
                columns = ('thread, turn_id, item_id, seq, earlier, events'
                           if table == 'external_item' else 'thread, turn_id, item_id, events')
                with closing(db.execute(f'SELECT rowid, {columns} FROM "{table}" ORDER BY rowid')) as rows:
                    for row in rows:
                        rowid, *values = row
                        raw = FieldCodec.parse(values[-1])
                        original = json.loads(json.dumps(raw))
                        updated, moved, new = _events(raw, table, rowid)
                        typed = FieldCodec.decode(tuple[TranscriptEvent, ...], updated)
                        encoded = declared.encode(typed)
                        if FieldCodec.parse(encoded) != updated:
                            raise ValueError(f'New Core changed {table} row {rowid}')
                        # Reverse ONLY the proven P502 tag move, leaving every other field exact.
                        for position in moved:
                            tool = updated[position]['tool']
                            tool['kind'] = tool.pop('tool')
                        if updated != original:
                            raise ValueError(f'Other content changed in {table} row {rowid}')
                        identities.update(repr((table, rowid, *values[:-1])).encode())
                        counts[f'{table}_rows'] += 1
                        counts['old_tool_parts'] += len(moved)
                        counts['new_tool_parts'] += new
                        if moved:
                            changes[table].append((encoded, rowid))
            if counts['old_tool_parts'] and counts['new_tool_parts']:
                raise ValueError('Mixed old and new stored tool tags; refusing partial conversion')
            if counts['old_tool_parts']:
                env = dict(os.environ)
                env.pop('PYTHONPATH', None)
                proof = subprocess.run(
                    [str(old_python), '-B', '-c', OLD_PROOF, str(path)],
                    check=True, capture_output=True, text=True, env=env,
                )
                if json.loads(proof.stdout) != {table: counts[f'{table}_rows'] for table in fields}:
                    raise ValueError('Old Core checked a different set of rows')
            if apply:
                for table, rows in changes.items():
                    db.executemany(f'UPDATE "{table}" SET events=? WHERE rowid=?', rows)
                for table, declared in fields.items():
                    row_type = ExternalItemRow if table == 'external_item' else ExternalDraftRow
                    with closing(row_type.stream(db)) as rows:
                        if sum(1 for _ in rows) != counts[f'{table}_rows']:
                            raise ValueError(f'New Core lost a {table} row')
                db.commit()
            else:
                db.rollback()
        except BaseException:
            db.rollback()
            raise
    return {
        'database': str(path), 'applied': apply,
        'rows': {table: counts[f'{table}_rows'] for table in fields},
        'old_tool_parts': counts['old_tool_parts'],
        'already_new_tool_parts': counts['new_tool_parts'],
        'changed_rows': {table: len(rows) for table, rows in changes.items()},
        'identity_sha256': identities.hexdigest(),
    }


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('root', type=Path, help='root containing external_transcripts.sqlite3')
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--old-python', type=Path, default=OLD_PYTHON)
    args = parser.parse_args()
    path = ExternalTranscript.path_in(args.root)
    print(json.dumps(convert(path, apply=args.apply, old_python=args.old_python), indent=2))
