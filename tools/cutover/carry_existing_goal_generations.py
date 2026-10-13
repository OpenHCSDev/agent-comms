"""One-time #777 carry. Preview reads the live ledger without opening a writer.

Public blocked/paused/completed goal state is not private launch state. A goal
with no recorded attempt has an unreserved READY generation; its public block
still prevents scheduling. Retry requires the existing explicit owner decision.
No input, attempt, public goal or executing-owner identity is changed here.
"""
from __future__ import annotations

import argparse
from contextlib import closing
import json
from pathlib import Path
import sqlite3

from agent_comms.field_codec import FieldCodec
from agent_comms.goal_attempts import AttemptRecord, Generation, assert_goal_attempt_schema
from agent_comms.registry_store import RegistryStore
from agent_comms.store_files import _store_lock


def preview(root: Path) -> dict:
    path = root / 'goal-private/goal_attempts.sqlite3'
    if not path.is_file():
        raise ValueError('Existing private goal ledger is required; preview never initializes it')
    with _store_lock(root / 'wire', shared=True), closing(sqlite3.connect(
        path.as_uri() + '?mode=ro', uri=True, isolation_level=None, timeout=5
    )) as conn:
        conn.execute('PRAGMA query_only=ON')
        assert_goal_attempt_schema(conn)
        document = RegistryStore(root / 'registry.json').read()
        goals, missing = [], []
        for thread in document.threads.values():
            goal = thread.goal
            if goal is None:
                continue
            generation = Generation.read_in(conn, goal.id)
            attempts = AttemptRecord.select(conn, where='goal_id=?', parameters=(goal.id,))
            row = {'thread': thread.name, 'owner_created_at': thread.created_at,
                   'goal_id': goal.id, 'goal_revision': goal.revision,
                   'state': goal.state.declared_name, 'progress': goal.progress,
                   'executing': thread.executing,
                   'generation': FieldCodec.encode(generation) if generation else None,
                   'attempts': len(attempts)}
            goals.append(row)
            if generation is None:
                if attempts:
                    raise ValueError(f'{thread.name}: attempts exist without a generation; refuse carry')
                missing.append(row)
        return {'root': str(root), 'missing_count': len(missing), 'would_carry': missing,
                'paper1_editor': next((row for row in goals if row['thread'] == 'paper1-editor'), None),
                'existing_goals': goals, 'mode': 'read-only'}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(preview(args.root.resolve(strict=True)), indent=2))
