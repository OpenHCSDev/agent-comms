"""Target-format transaction, held under the inherited original writer custody."""
from contextlib import closing, nullcontext
import json
import os
from pathlib import Path
import sqlite3
import sys

from agent_comms.private_bus_checkpoint import install_private_bus_checkpoint
from agent_comms.store_files import _atomic_write_text
from agent_comms.registry_store import RegistryStore
from agent_comms.transcript_routes import _assert_schema
from install_retained_index import require_retained_writer
from routing_carry import digest_json, file_witness, registry_without_routing, tables, table_rows
from validate_retained_routing import read_plan


def main():
    root, descriptor, root_id, receipt = sys.argv[1:]
    root, plan = read_plan(root, descriptor, root_id)
    receipt = Path(receipt)
    if receipt.exists():
        raise ValueError('A retained original receipt cannot be overwritten.')
    bus = require_retained_writer(root, descriptor, root_id)
    database = root / 'transcript_routes.sqlite3'
    connection_scope = (closing(sqlite3.connect(database.as_uri() + '?mode=rw', uri=True))
                        if database.exists() else nullcontext(None))
    original_registry = (root / 'registry.json').read_text()
    registry_store = RegistryStore(root / 'registry.json')
    registry_mode = (root / 'registry.json').stat().st_mode & 0o777
    with connection_scope as connection:
        if connection is not None:
            connection.execute('PRAGMA synchronous=FULL')
            connection.execute('BEGIN IMMEDIATE')
        document = plan.require_target(root, connection)
        try:
            for cell in plan.cells:
                table = next(table for table in tables() if table.declared_name == cell.table)
                where = ' AND '.join(f'"{key}"=?' for key in cell.keys)
                encoded = json.dumps(cell.routing, separators=(',', ':'))
                changed = connection.execute(
                    f'UPDATE "{table.declared_name}" SET routing=? WHERE {where} AND routing IS NOT NULL',
                    (encoded, *cell.keys.values()),
                ).rowcount
                if changed != 1:
                    raise ValueError('Original routing cell was not updated exactly once.')
            for turn in plan.turns:
                document['threads'][turn.thread]['active_turn']['routing'] = turn.routing
            if plan.turns:
                # The original writer holds the registry lock. Its existing
                # durability owner prepares/commits the exact replacement;
                # no target reader decodes the original embedded requests.
                registry_store._write_unlocked(json.dumps(document))
                os.chmod(root / 'registry.json', registry_mode)
            if connection is not None:
                # Inspect every post-image while rollback remains available.
                _assert_schema(connection)
                after = {table.declared_name: [{k: v for k, v in row.items() if k != 'routing'}
                         for row in table_rows(connection, table)] for table in tables()}
                if digest_json(after) != plan.annotation_nonrouting_digest:
                    raise ValueError('Carry changed nonrouting annotation data.')
                connection.commit()
        except BaseException:
            if connection is not None:
                connection.rollback()
            if plan.turns and (root / 'registry.json').read_text() != original_registry:
                # A failed pending guard deliberately refuses recovery here.
                # The stopped batch and retained preimages remain for review.
                registry_store._write_unlocked(original_registry)
                os.chmod(root / 'registry.json', registry_mode)
            raise
    # Rebuild only the derived index with the same opened original descriptor.
    witness = install_private_bus_checkpoint(bus, _bus_locked=True)
    plan.require_sources(root)
    registry = json.loads((root / 'registry.json').read_bytes())
    if digest_json(registry_without_routing(registry)) != plan.registry_nonrouting_digest:
        raise ValueError('Carry changed original nonrouting registry state.')
    if database.exists() and file_witness(database)['mode'] != plan.database['mode']:
        raise ValueError('Carry changed annotation storage mode.')
    result = {'root_id': root_id, 'through_seq': witness.through_seq,
              'original_writer_descriptor_verified': True,
              'routing_cells': len(plan.cells), 'registry_routings': len(plan.turns),
              'original_bus': plan.bus, 'protected_originals': plan.protected,
              'original_certified_references': plan.originals,
              'original_registry_sha256': plan.registry_digest,
              'original_annotation_database': plan.database,
              'carried_annotation_database': file_witness(database),
              'nonrouting_annotations_unchanged': True, 'nonrouting_registry_unchanged': True,
              'original_input_replays': 0, 'provider_calls': 0}
    _atomic_write_text(receipt, json.dumps(result, indent=2) + '\n', fsync_parent=True)
    print(json.dumps({'receipt': str(receipt), 'routing_cells': len(plan.cells),
                      'registry_routings': len(plan.turns)}), flush=True)


if __name__ == '__main__':
    main()
