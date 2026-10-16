"""Exercise a retained native reader through normal interpreter shutdown."""
import atexit
import json
import multiprocessing
import sqlite3
import sys
from pathlib import Path

from agent_comms.view_unread import TranscriptReadState


reader = None


def report(path):
    database_closed = True
    if reader._connection is not None:
        try:
            reader._connection.execute('SELECT 1')
        except sqlite3.ProgrammingError:
            pass  # The retained original connection was closed before this callback.
        else:
            database_closed = False
    path.write_text(json.dumps({
        'cancelled': reader._cancelled.is_set(),
        'database_closed': database_closed,
        'preparation_done': reader.preparation is None or reader.preparation.done(),
        'active_children': len(multiprocessing.active_children()),
    }))


if __name__ == '__main__':
    output, mode = Path(sys.argv[1]), sys.argv[2]
    output.mkdir(mode=0o700)
    # Registered before creating the reader: its resource cleanup must precede
    # this callback, not wait until module globals are destroyed afterward.
    atexit.register(report, output / 'exit.json')
    reader = TranscriptReadState(output / 'read_ledger.json')
    if mode == 'active':
        source = output / 'session.jsonl'
        source.write_text(json.dumps({
            'type': 'message',
            'message': {'role': 'assistant', 'content': [{'type': 'text', 'text': 'Reply'}]},
        }) + '\n')
        reader.counts('viewer', {'worker': str(source)})
    # Deliberately retain the reader and omit close(), just as the installer did.
