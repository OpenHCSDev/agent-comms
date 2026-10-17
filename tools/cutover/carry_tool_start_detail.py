"""Copy a stopped external transcript store, dropping the retired start preview.

Run with the reviewed target interpreter. Source is opened read-only; output
must not exist. Publish output only through the existing stopped cutover after
this command succeeds. No session, input-disposition or native journal is read
or changed. Delete this one-shot tool after the durable cutover.
"""

from __future__ import annotations

import argparse
from contextlib import closing
import json
import os
from pathlib import Path
import sqlite3

from agent_comms.external_transcript import ExternalTranscript
from agent_comms.field_codec import FieldCodec
from agent_comms.transcript_events import ToolStartTranscript


def carry(source: Path, output: Path) -> dict:
    source = source.resolve(strict=True)
    output = output.absolute()
    # Exclusive creation also refuses aliases and symlinks to an existing store.
    descriptor = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    os.close(descriptor)
    result = {}
    with closing(sqlite3.connect(source.as_uri() + '?mode=ro', uri=True)) as original, \
            closing(sqlite3.connect(output)) as converted:
        original.execute('PRAGMA query_only = ON')
        original.execute('BEGIN')
        original.execute('SELECT name FROM sqlite_master').fetchall()
        original.backup(converted)
        with converted:
            for row_type in ExternalTranscript.rows:
                table = f'"{row_type.declared_name}"'
                keys = tuple(item.name for item in row_type._fields() if item.column.primary_key)
                columns = row_type.columns()
                selection = ', '.join(f'"{name}"' for name in columns)
                order = ', '.join(f'"{name}"' for name in keys)
                where = ' AND '.join(f'"{name}" = ?' for name in keys)
                rows = removed = changed = 0
                cursor = original.execute(f'SELECT {selection} FROM {table} ORDER BY {order}')
                try:
                    for values in cursor:
                        rows += 1
                        stored = dict(zip(columns, values, strict=True))
                        events = json.loads(stored['events'])
                        count = 0
                        for event in events:
                            if (event.get(ToolStartTranscript.family_discriminator)
                                    == ToolStartTranscript.declared_name and 'detail' in event):
                                FieldCodec.decode(str, event.pop('detail'))
                                count += 1
                        if count:
                            update = converted.execute(
                                f'UPDATE {table} SET events = ? WHERE {where}',
                                (json.dumps(events, separators=(',', ':')), *(stored[key] for key in keys)))
                            if update.rowcount != 1:
                                raise ValueError(f'Expected one original {row_type.__name__} row')
                            removed += count
                            changed += 1
                finally:
                    cursor.close()
                # Use the actual target row decoder, including every event and diff.
                decoded = sum(1 for _ in row_type.iterate(converted.execute(
                    f'SELECT {selection} FROM {table} ORDER BY {order}')))
                if decoded != rows:
                    raise ValueError(f'Row count changed for {row_type.__name__}')
                result[row_type.__name__] = dict(rows=rows, decoded=decoded,
                                                changed_rows=changed, removed_details=removed)
        if converted.execute('PRAGMA integrity_check').fetchone() != ('ok',):
            raise ValueError('Converted transcript database failed integrity_check')
    return dict(source=str(source), output=str(output), tables=result)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    arguments = parser.parse_args()
    print(json.dumps(carry(arguments.source, arguments.output), indent=2))


if __name__ == '__main__':
    main()
