"""P262 one-shot storage carry; quiesce old registry readers/writers first.

Run BEFORE carry_goal_publications.py, then carry_goal_wait_owners.py. Only
storage fields are decoded here: legacy Goal documents remain byte-for-value
unchanged until their own carry. The existing registry writer preserves the
private guard protocol. Dry-run is the default. No sessions or input rows change.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from agent_comms.field_codec import FieldCodec
from agent_comms.registry_store import RegistryStore
from agent_comms.storage_rules import StorageRoots, StorageRule
from agent_comms.store_files import _store_lock


def carry(root: Path, *, apply: bool = False) -> dict:
    store = RegistryStore(root / 'registry.json')
    with _store_lock(root / 'wire'), store.locked():
        store.private_guard_unlocked()
        document = json.loads(store.path.read_text())
        rule = StorageRule.load(root)
        changed = 0
        for name, row in document['threads'].items():
            old = dict(row)
            paths = [*row.get('previous_worktrees', ()), row['worktree']]
            paths = tuple(dict.fromkeys(Path(path).expanduser() for path in paths))
            if any(not path.is_absolute() for path in paths):
                raise ValueError(f'{name}: legacy project path must be absolute')
            stored = row.get('storage_roots')
            roots = (FieldCodec.decode(StorageRoots, stored) if stored is not None
                     else rule.initial_roots(name, str(paths[0])))
            for path in paths:
                roots = roots.adding(path.resolve())
            row['storage_roots'] = FieldCodec.encode(roots)
            row.pop('previous_worktrees', None)
            changed += row != old
        if apply and changed:
            # Do not decode RegistryDocument here: its Goal schema is carried next.
            # This is the existing guarded writer, not an alternate file protocol.
            store._write_unlocked(json.dumps(document, indent=2))
    return {'threads': len(document['threads']), 'carried_records': changed, 'applied': apply}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('root', type=lambda text: Path(text).resolve())
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    print(json.dumps(carry(args.root, apply=args.apply)))


if __name__ == '__main__':
    main()
