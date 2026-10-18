"""Storage roots without a checkout list (P430).

Storage roots keep only the scratch root; the projects a thread left move to
earlier_projects. Every registry row must decode before the registry is written.
Before an installation that still reads saved JSON releases stops its owners,
those releases get the checkout list it expects.
"""
from __future__ import annotations

import copy
import json
import os
import sqlite3
from pathlib import Path

from agent_comms.field_codec import FieldCodec
from agent_comms.registry_store import RegistryStore
from agent_comms.storage_rules import StorageRoots, StorageRule
from agent_comms.store_files import _store_lock
from agent_comms.threads import Thread

from cutover.carry import Carried, Carry


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
    """Run with the running installation's src: a release it already reads is left alone."""
    try:
        FieldCodec.decode(Thread, row)
        return
    except (ValueError, TypeError, KeyError):
        pass
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
    history = root / 'goal_history.sqlite3'
    if history.exists():
        with sqlite3.connect(history.as_uri() + '?mode=ro', uri=True) as db:
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


def decoded(rows: dict, convert, rule: StorageRule, published: dict) -> int:
    """Convert every row in place; all must decode as this src's Thread."""
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
    if failures:
        raise ValueError(json.dumps(failures, sort_keys=True))
    return changed


class StorageRootsScratch(Carry):
    """Storage roots keep only the scratch root; projects a thread left move to earlier_projects."""

    after = None

    @classmethod
    def carry(cls, root: Path, *, apply: bool) -> Carried:
        registry = RegistryStore(root / 'registry.json')
        with registry.locked():
            if apply:
                registry.private_guard_unlocked()
            document = json.loads(registry.path.read_text())
            changed = decoded(document['threads'], carry_thread, StorageRule.load(root),
                                 published_goals(root, document))
            FieldCodec.decode(registry.record_type, document)
            if apply and changed:
                registry._write_unlocked(json.dumps(document, indent=2))
        return Carried(changed, {'threads': len(document['threads'])},
                       (registry.path.name, registry.guard_path.name) if changed else ())

    @classmethod
    def before_stop(cls, root: Path, *, apply: bool) -> Carried:
        """Saved JSON releases written before checkouts were recorded get them, so a
        running installation that still reads that file can stop its owners."""
        path = root / 'owner_release_receipts.json'
        if not path.exists():
            return Carried(0)
        with _store_lock(path):
            document = json.loads(path.read_text())
            changed = decoded({name: record['thread'] for name, record in document.items()},
                                 prepare_release, StorageRule.load(root), {})
            if apply and changed:
                stage = path.with_name(f'.{path.name}.carry')
                stage.write_text(json.dumps(document, indent=2))
                stage.chmod(0o600)
                os.replace(stage, path)
        return Carried(changed, {'releases': len(document)}, (path.name,) if changed else ())


if __name__ == '__main__':
    StorageRootsScratch.main()
