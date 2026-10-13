"""One-time #777 carry; delete after the backup-first live carry.

The newly carried generation is unreserved; zero recorded attempts says nothing
about the goal's past execution. Public blocks still require explicit Retry;
pauses and completion still prevent scheduling. No attempt, input, public goal,
executing-owner identity or existing generation is changed here. No bus copy.
"""
from __future__ import annotations

import argparse
from contextlib import closing
import json
import os
from pathlib import Path
import sqlite3

from agent_comms.goal_attempts import (
    AttemptRecord, Generation, GoalAttemptStore, assert_goal_attempt_schema,
)
from agent_comms.registry_store import RegistryStore
from agent_comms.store_files import _store_lock


def readonly(root: Path):
    path = root / 'goal-private/goal_attempts.sqlite3'
    if not path.is_file():
        raise ValueError('Existing private ledger required; never initialize during carry')
    conn = sqlite3.connect(path.as_uri() + '?mode=ro', uri=True,
                           isolation_level=None, timeout=5)
    conn.execute('PRAGMA query_only=ON')
    return conn


def capture(root: Path):
    with _store_lock(root / 'wire', shared=True), closing(readonly(root)) as conn:
        assert_goal_attempt_schema(conn)
        document = RegistryStore(root / 'registry.json').read()
        rows = []
        for thread in document.threads.values():
            if thread.goal is None:
                continue
            generation = Generation.read_in(conn, thread.goal.id)
            attempts = len(AttemptRecord.select(conn, where='goal_id=?',
                                                parameters=(thread.goal.id,)))
            if generation is None and attempts:
                raise ValueError(f'{thread.name}: attempts without generation; refuse carry')
            rows.append((thread, generation, attempts))
        return document, rows


def report(root: Path, document, rows):
    goals = []
    for thread, generation, attempts in rows:
        goal = thread.require_goal()
        goals.append({'thread': thread.name, 'owner_created_at': thread.created_at,
                      'goal_id': goal.id, 'goal_revision': goal.revision,
                      'state': goal.state.declared_name, 'progress': goal.progress,
                      'status': document.statuses[thread.name].declared_name,
                      'executing': thread.executing,
                      'generation': generation.number if generation else None,
                      'lifecycle': generation.lifecycle.declared_name if generation else None,
                      'attempt_id': generation.attempt_id if generation else None,
                      'attempts': attempts})
    missing = [row for row in goals if row['generation'] is None]
    return {'root': str(root), 'missing_count': len(missing), 'would_carry': missing,
            'paper1_editor': next((row for row in goals if row['thread'] == 'paper1-editor'), None),
            'existing_goals': goals, 'mode': 'read-only'}


def preview(root: Path):
    return report(root, *capture(root))


def save_receipt(path: Path, receipt):
    with path.open('w') as output:
        json.dump(receipt, output, indent=2)
        output.write('\n')
        output.flush()
        os.fsync(output.fileno())


def carry(root: Path, backup: Path):
    document, rows = capture(root)
    pending = [thread for thread, generation, _ in rows if generation is None]
    before = report(root, document, rows)
    # This destination must be new. SQLite backup runs OUTSIDE the wire lock;
    # nothing reads or copies the append-only bus.
    backup.mkdir(mode=0o700, parents=True, exist_ok=False)
    destination = backup / 'goal_attempts.sqlite3'
    destination.touch(mode=0o600, exist_ok=False)
    with closing(readonly(root)) as source, closing(sqlite3.connect(destination)) as target:
        source.backup(target, pages=64)
        assert_goal_attempt_schema(target)
        assert target.execute('PRAGMA integrity_check').fetchone() == ('ok',)
    GoalAttemptStore._sync_paths(destination, backup)
    receipt = {'root': str(root), 'backup': str(destination), 'before': before,
               'writes': [], 'carried_count': 0}
    result_path = backup / 'receipt.json'
    save_receipt(result_path, receipt)
    store = GoalAttemptStore(root / 'goal-private')
    registry = RegistryStore(root / 'registry.json')
    for expected in pending:
        goal = expected.require_goal()
        intent = {'thread': expected.name, 'goal_id': goal.id, 'result': 'not-issued'}
        receipt['writes'].append(intent)
        with _store_lock(root / 'wire'):
            current = registry.read().threads.get(expected.name)
            if current is None or current.incarnation != expected.incarnation or current.goal != goal:
                intent['result'] = 'skipped-changed-goal-or-owner'
            elif store.snapshot(goal.id) is not None:
                intent['result'] = 'skipped-existing-generation'
            else:
                with closing(readonly(root)) as conn:
                    if AttemptRecord.select(conn, where='goal_id=?', parameters=(goal.id,)):
                        raise ValueError('Attempt appeared without generation; refuse carry')
                # Persist intent before issuing a write. A failed/uncertain
                # durability result must be inspected, never blindly rerun.
                intent['result'] = 'issued-awaiting-readback'
                save_receipt(result_path, receipt)
                generation = store.create_goal(goal.id)
                assert generation.number == 1 and generation.attempt_id is None
                generation.lifecycle.require_ready()
                intent['result'] = 'recorded-unreserved-generation-1'
                receipt['carried_count'] += 1
        save_receipt(result_path, receipt)
    receipt['after'] = preview(root)
    save_receipt(result_path, receipt)
    # No process-local grant leaves this tool; explicit Retry/recovery remains
    # with the executing owner. Public status and retained input are untouched.
    return receipt


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, required=True)
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--backup', type=Path)
    args = parser.parse_args()
    root = args.root.resolve(strict=True)
    if args.apply:
        if args.backup is None:
            parser.error('--apply requires a new --backup directory')
        result = carry(root, args.backup.resolve())
    else:
        result = preview(root)
    print(json.dumps(result, indent=2))
