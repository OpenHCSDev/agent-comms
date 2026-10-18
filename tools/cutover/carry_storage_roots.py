"""P430 one-time registry carry. Stop old registry writers before --apply.

storage_roots loses its checkout list; the projects a thread left move to earlier_projects. The existing RegistryStore writer maintains the
private guard. Session files, wire history and input attempts are not opened.
Dry-run is the default; production has no reader for the old checkout list.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

from agent_comms.field_codec import FieldCodec
from agent_comms.registry_store import RegistryStore
from agent_comms.storage_rules import StorageRoots


def carry(path: Path, *, apply: bool = False) -> dict:
    store = RegistryStore(path)
    with store.locked():
        store.private_guard_unlocked()
        original = store.path.read_text()
        document = json.loads(original)
        changed = []
        for name, row in document['threads'].items():
            old = row['storage_roots']
            roots = FieldCodec.decode(StorageRoots, {'scratch': old['scratch']})
            row['storage_roots'] = FieldCodec.encode(roots)
            # The checkout list was also the record of projects this thread left.
            current = str(Path(row['worktree']).expanduser().resolve())
            earlier = list(dict.fromkeys((*row.get('earlier_projects', ()),
                                          *(p for p in old.get('checkouts', ()) if p != current))))
            if earlier:
                row['earlier_projects'] = earlier
            if old != row['storage_roots']:
                changed.append(name)
        proposed = json.dumps(document, indent=2)
        if apply and changed:
            store._write_unlocked(proposed)
    return {'registry': str(path), 'threads': len(document['threads']),
            'changed': len(changed), 'applied': apply,
            'before_sha256': hashlib.sha256(original.encode()).hexdigest(),
            'after_sha256': hashlib.sha256(proposed.encode()).hexdigest()}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('registry', type=lambda value: Path(value).expanduser().resolve())
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    print(json.dumps(carry(args.registry, apply=args.apply), sort_keys=True))


if __name__ == '__main__':
    main()
