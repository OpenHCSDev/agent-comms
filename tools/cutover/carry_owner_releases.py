"""Carry old owner-release JSON into the P434 table with all writers stopped."""
from __future__ import annotations

import argparse
import json
from contextlib import closing
from pathlib import Path

from agent_comms.coordination_database import CoordinationStore
from agent_comms.field_codec import FieldCodec
from agent_comms.owner_lifecycle import OwnerReleaseEvidence, OwnerReleaseStore
from agent_comms.store_files import _store_lock


def carry(root: Path, *, apply: bool = False) -> dict:
    source = root / 'owner_release_receipts.json'
    destination = OwnerReleaseStore.path_in(root)
    with _store_lock(root / 'wire'), _store_lock(source):
        records = json.loads(source.read_text())
        prepared = {}
        failures = []
        for name, record in records.items():
            try:
                thread = record['thread']
                if name != thread['name']:
                    raise ValueError('Release key differs from thread name')
                prepared[name] = FieldCodec.decode_record(OwnerReleaseEvidence, {
                    'name': name, 'created_at': thread['created_at'],
                    'process_identity': thread['process_identity'],
                    'before': record['before'], 'after': record['after'],
                })
            except (ValueError, TypeError, KeyError) as error:
                failures.append({'name': name, 'error': str(error)})
        existing = {}
        if destination.exists():
            with closing(CoordinationStore.read_snapshot(destination)) as db:
                existing = {row.name: row for row in OwnerReleaseEvidence.select(db)}
            for name in prepared.keys() & existing.keys():
                if prepared[name] != existing[name]:
                    failures.append({'name': name, 'error': 'Existing SQL release differs; refusing overwrite'})
        report = {'rows': len(records), 'existing_rows': len(existing),
                  'changed': len(prepared.keys() - existing.keys()),
                  'decode_failures': failures, 'applied': False}
        if failures:
            return report
        if apply:
            # Decode and compare every old and new row before creating or writing the store.
            store = OwnerReleaseStore(root)
            with store.writing() as db:
                for name in prepared.keys() - existing.keys():
                    prepared[name].upsert(db)
            report['applied'] = True
        return report


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('root', type=lambda value: Path(value).resolve())
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    result = carry(args.root, apply=args.apply)
    print(json.dumps(result, sort_keys=True))
    raise SystemExit(bool(result['decode_failures']))
