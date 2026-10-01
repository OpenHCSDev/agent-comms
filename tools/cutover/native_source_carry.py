"""One-shot native schema 4 -> current source-relation carry, outside runtime.

The stopped-owner installer calls prepare_original under the original interpreter
and carry_into under the target interpreter on its disposable COPIED coordinator.
The installer owns stopped custody, originals, activation and rollback. This tool
never touches journals, native processes, public pointers or the original root.
Delete this transient bridge after the operator completes the cutover.
"""
from contextlib import closing
import hashlib
import json
from pathlib import Path
import sqlite3

from agent_comms.coordinated_runtime_schema import assert_native_runtime_schema
from agent_comms.coordination_tables.assignments import WakeAssignment
from agent_comms.coordinator import Coordination
from agent_comms.native_prompt_binding import PromptBinding
from agent_comms.native_runtime_input import CurrentNativeCursor, NativeRuntimeInput, NativeRuntimeSchemaMeta
from agent_comms.private_sidecar import create_sidecar_file, sidecar_connection
from agent_comms.typed_table import SQLiteSchemaObject

from routing_carry import file_witness


def _cells(db, table):
    columns = table.columns()
    rows = [list(row) for row in db.execute(
        f'SELECT {table._column_list(columns)} FROM "{table.declared_name}"'
    )]
    return {'columns': list(columns), 'rows': sorted(rows, key=json.dumps)}


def _non_native_digest(db):
    """Opaque SQLite cells, not target decoding of original lifecycle records."""
    replaced = {table.declared_name for table in (
        NativeRuntimeSchemaMeta, NativeRuntimeInput, CurrentNativeCursor,
    )}
    objects = SQLiteSchemaObject.read(db.execute(
        "SELECT name,sql FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
    ))
    digest = hashlib.sha256()
    for original in sorted(objects, key=lambda item: item.name):
        if original.name in replaced or original.name.startswith('native_runtime_'):
            continue
        name = '"' + original.name.replace('"', '""') + '"'
        rows = sorted((list(row) for row in db.execute(f'SELECT * FROM {name}')), key=json.dumps)
        digest.update(json.dumps((original.name, original.sql, rows), separators=(',', ':')).encode())
    return digest.hexdigest()


def prepare_original(root):
    """Read schema four ONLY with its authentic owner declarations; never install."""
    root = Path(root).resolve(strict=True)
    database = root / 'coordination.sqlite3'
    binding_path = root / 'native_prompt_bindings.sqlite3'
    protected = {str(path): file_witness(path) for path in (
        database, database.with_name(database.name + '-wal'), binding_path,
    )}
    with closing(sqlite3.connect(database.as_uri() + '?mode=ro', uri=True)) as db:
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA foreign_keys=ON')
        db.execute('BEGIN')
        assert_native_runtime_schema(db)
        if NativeRuntimeSchemaMeta.one(db, singleton=1).version != 4:
            raise ValueError('The original interpreter must own the exact schema-four source')
        if db.execute("SELECT 1 FROM sqlite_master WHERE name='selected_native_sources'").fetchone():
            raise ValueError('Prototype batch originals need their own explicit attestation; not scalar source four')
        inputs = NativeRuntimeInput.select(db)
        by_id = {original.input_id: original for original in inputs}
        for original in inputs:
            original.reference  # Original owner rejects partial context, without journal reconstruction.
        for cursor in CurrentNativeCursor.select(db):
            if cursor.injected_seq:
                original = by_id[cursor.input_id]
                source = WakeAssignment.one(db, assignment_id=cursor.assignment_id)
                if cursor.reference != original.reference or source.wire_seq != cursor.injected_seq:
                    raise ValueError('Original cursor does not name its original injected source')
        bindings = {'columns': list(PromptBinding.columns()), 'rows': []}
        if not protected[str(binding_path)].get('missing'):
            with sidecar_connection(binding_path, PromptBinding) as sidecar:
                for binding in PromptBinding.select(sidecar):
                    original = by_id[binding.input_id]
                    source = WakeAssignment.one(db, assignment_id=original.assignment_id)
                    if binding.identity != original.identity or binding.source != source.source:
                        raise ValueError('Original binding does not join its original source/input')
                bindings = _cells(sidecar, PromptBinding)
        packet = {
            'source_root': str(root), 'protected': protected,
            'inputs': _cells(db, NativeRuntimeInput),
            'cursors': _cells(db, CurrentNativeCursor), 'bindings': bindings,
            'non_native_digest': _non_native_digest(db),
        }
    for path, witness in protected.items():
        if file_witness(Path(path)) != witness:
            raise ValueError('Original storage changed during source export')
    return packet


def _project_original(db, table, original, removed):
    """Copy surviving RAW cells; current declarations decode them afterwards."""
    columns = table.columns()
    source_columns = original['columns']
    if set(source_columns) != set(columns) | set(removed):
        raise ValueError(f'Unexpected source-format fields for {table.declared_name}')
    positions = tuple(source_columns.index(column) for column in columns)
    projected = [tuple(row[index] for index in positions) for row in original['rows']]
    db.executemany(
        f'INSERT INTO "{table.declared_name}" ({table._column_list(columns)}) '
        f'VALUES ({",".join("?" for _ in columns)})', projected,
    )
    expected = {'columns': list(columns), 'rows': sorted((list(row) for row in projected), key=json.dumps)}
    if _cells(db, table) != expected:
        raise ValueError(f'Original surviving cells changed for {table.declared_name}')
    table.select(db)  # Strict acquisition by the current declaration, no old reader.


def carry_into(staged_root, packet, expected_stage_non_native_digest):
    """Produce a reviewed native DB plus binding artifact; do NOT activate them."""
    staged_root = Path(staged_root).resolve(strict=True)
    original_root = Path(packet['source_root']).resolve(strict=True)
    if staged_root == original_root:
        raise ValueError('Native carry only writes an installer-owned disposable copy')
    for path, witness in packet['protected'].items():
        if file_witness(Path(path)) != witness:
            raise ValueError('Original storage changed before native carry')
    database = staged_root / 'coordination.sqlite3'
    original_database = packet['protected'][str(original_root / 'coordination.sqlite3')]
    copied_database = file_witness(database)
    if (copied_database['device'], copied_database['inode']) == (
        original_database['device'], original_database['inode']
    ):
        raise ValueError('Staged coordinator aliases original storage')
    output_binding = staged_root / 'native-source-carry-bindings.sqlite3'
    if output_binding.exists() or output_binding.is_symlink():
        raise ValueError('Carry artifact already exists; inspect the prior attempt, do not overwrite')
    with Coordination(str(database)) as store:
        db = store.session._connection
        if _non_native_digest(db) != expected_stage_non_native_digest:
            raise ValueError('Copied coordinator differs from the reviewed installer stage')
        db.execute('PRAGMA foreign_keys=OFF')
        try:
            with store.session.transaction():
                for table, original in ((NativeRuntimeInput, packet['inputs']), (CurrentNativeCursor, packet['cursors'])):
                    names = _original_columns(original['columns'])
                    rows = [list(row) for row in db.execute(f'SELECT {names} FROM "{table.declared_name}"')]
                    if sorted(rows, key=json.dumps) != original['rows']:
                        raise ValueError('Copied original native cells differ before carry')
                for table in (CurrentNativeCursor, NativeRuntimeInput, NativeRuntimeSchemaMeta):
                    db.execute(f'DROP TABLE "{table.declared_name}"')
                NativeRuntimeSchemaMeta.create_schema(db)
                _project_original(db, NativeRuntimeInput, packet['inputs'], ('assignment_id',))
                source_columns = packet['inputs']['columns']
                for cells in packet['inputs']['rows']:
                    input_id = cells[source_columns.index('input_id')]
                    source_id = cells[source_columns.index('assignment_id')]
                    source = WakeAssignment.one(db, assignment_id=source_id)
                    if source is None:
                        raise ValueError('Original native source disappeared from copied coordinator')
                    native = NativeRuntimeInput.one(db, input_id=input_id)
                    native.execution.record_sources(db, input_id, (source,))
                _project_original(db, CurrentNativeCursor, packet['cursors'], ('assignment_id',))
                if _non_native_digest(db) != expected_stage_non_native_digest:
                    raise ValueError('Native carry changed non-native coordinator cells')
                if list(db.execute('PRAGMA foreign_key_check')):
                    raise ValueError('Carried native relations violate original references')
        finally:
            db.execute('PRAGMA foreign_keys=ON')
        assert_native_runtime_schema(db)
    create_sidecar_file(output_binding, PromptBinding)
    with sidecar_connection(output_binding, PromptBinding) as sidecar:
        _project_original(sidecar, PromptBinding, packet['bindings'], ('assignment_id', 'source_seq', 'message_id'))
    for path, witness in packet['protected'].items():
        if file_witness(Path(path)) != witness:
            raise ValueError('Original storage changed while writing copied carry')
    return {'coordinator': str(database), 'binding_artifact': str(output_binding),
            'original_inputs': len(packet['inputs']['rows']),
            'original_cursors': len(packet['cursors']['rows']),
            'original_bindings': len(packet['bindings']['rows']),
            'originals_unchanged': True, 'activated': False}


def _original_columns(columns):
    return ','.join('"' + name.replace('"', '""') + '"' for name in columns)
