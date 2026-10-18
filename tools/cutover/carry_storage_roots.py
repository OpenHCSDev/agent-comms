"""Carry storage fields in the registry and saved owner releases together.

Dry-run copies first. Stop old writers before applying the new registry shape.
All records must decode before either store is written. Sessions and input
attempts are not opened or changed.
"""
from __future__ import annotations

import argparse
import copy
import json
import sqlite3
from pathlib import Path

from agent_comms.field_codec import FieldCodec
from agent_comms.owner_lifecycle import OwnerReleaseStore
from agent_comms.registry_store import RegistryStore
from agent_comms.storage_rules import StorageRoots, StorageRule
from agent_comms.store_files import _store_lock
from agent_comms.threads import Thread


def carry_thread(row: dict, rule: StorageRule) -> None:
    old = row.get('storage_roots', {})
    scratch = old.get('scratch', str(rule.scratch_base / row['name']))
    roots = FieldCodec.decode(StorageRoots, {'scratch': scratch})
    current = str(Path(row['worktree']).expanduser().resolve())
    paths = (*row.get('earlier_projects', ()), *row.pop('previous_worktrees', ()),
             *old.get('checkouts', ()))
    if any(not Path(path).is_absolute() for path in paths):
        raise ValueError('Stored project path must be absolute')
    earlier = list(dict.fromkeys(path for path in paths if path != current))
    row['storage_roots'] = FieldCodec.encode(roots)
    if earlier:
        row['earlier_projects'] = earlier


def prepare_release(row: dict, rule: StorageRule) -> None:
    """Run with the CURRENT interpreter so its stop command can read releases."""
    old = row.get('storage_roots', {})
    paths = (*old.get('checkouts', ()), *row.pop('previous_worktrees', ()), row['worktree'])
    roots = FieldCodec.decode(StorageRoots, {
        'scratch': old.get('scratch', str(rule.scratch_base / row['name'])),
        'checkouts': list(dict.fromkeys(str(Path(p).expanduser().resolve()) for p in paths)),
    })
    row['storage_roots'] = FieldCodec.encode(roots)


def published_goals(root: Path, registry: dict) -> dict:
    """Use the publication already carried for this identity, never a new cut."""
    goals = [row['goal'] for row in registry['threads'].values() if row.get('goal')]
    with sqlite3.connect((root / 'goal_history.sqlite3').as_uri() + '?mode=ro', uri=True) as db:
        for pair in db.execute('SELECT "before", "after" FROM goal_history_entry'):
            goals.extend(json.loads(raw) for raw in pair if raw is not None)
    published = {}
    for goal in goals:
        if goal is None:
            continue
        sequence = goal['published_seq']
        if published.setdefault(goal['id'], sequence) != sequence:
            raise ValueError(f"Conflicting publication for goal {goal['id']}")
    return published


def carry(path: Path, *, apply: bool = False, prepare_stop: bool = False) -> dict:
    registry = RegistryStore(path)
    releases = OwnerReleaseStore(path.with_name('owner_release_receipts.json'))
    root = path.parent
    with _store_lock(root / 'wire'), registry.locked(), releases.locked():
        if apply:
            registry.private_guard_unlocked()
        rule = StorageRule.load(root)
        originals = {registry.path: registry.path.read_text(),
                     releases.path: releases.path.read_text()}
        documents = {name: json.loads(text) for name, text in originals.items()}
        published = published_goals(root, documents[registry.path])
        sources = ((registry, documents[registry.path]['threads']),
                   (releases, {name: record['thread']
                               for name, record in documents[releases.path].items()}))
        convert = carry_thread
        if prepare_stop:
            sources = sources[1:]
            convert = prepare_release
        reports = {}
        for store, rows in sources:
            changed = 0
            failures = []
            for name, row in rows.items():
                before = copy.deepcopy(row)
                try:
                    convert(row, rule)
                    goal = row.get('goal')
                    if goal is not None and 'published_seq' not in goal:
                        goal['published_seq'] = published[goal['id']]
                    FieldCodec.decode(Thread, row)
                except (ValueError, TypeError, KeyError) as error:
                    failures.append({'thread': name, 'error': str(error)})
                changed += before != row
            if not failures:
                FieldCodec.decode(store.record_type, documents[store.path])
            reports[store.path.name] = {'rows': len(rows), 'changed': changed,
                                       'decode_failures': failures}
        clean = not any(report['decode_failures'] for report in reports.values())
        if apply and not clean:
            raise ValueError(json.dumps(reports, sort_keys=True))
        if apply:
            written = []
            try:
                for store, _ in sources:
                    if reports[store.path.name]['changed']:
                        store._write_unlocked(json.dumps(documents[store.path], indent=2))
                        written.append(store)
            except BaseException:
                for store in reversed(written):
                    store._write_unlocked(originals[store.path])
                raise
    return {'stores': reports, 'clean': clean, 'applied': apply}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('registry', type=lambda value: Path(value).expanduser().resolve())
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--prepare-stop', action='store_true',
                        help='Current runtime: repair old release records before stopping workers')
    args = parser.parse_args()
    result = carry(args.registry, apply=args.apply, prepare_stop=args.prepare_stop)
    print(json.dumps(result, sort_keys=True))
    if not result['clean']:
        raise SystemExit(1)


if __name__ == '__main__':
    main()
