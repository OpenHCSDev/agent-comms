"""One-use complete490 carry outside product readers, under stopped custody.

Original declarations authenticate the source. Current declarations own target
DDL. Only matched clones are transformed; original files remain preimages.
"""
from __future__ import annotations

from contextlib import ExitStack, closing
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import sys
from typing import Annotated, Any

from agent_comms.field_codec import FieldCodec, PathText
from agent_comms.private_path import PrivateDirectoryRole
from publish_openhcs_recovery import digest, fsync_directory, retain_file
from retained_summary_reset import AcquiredRuntimeFiles, RuntimeCompactionFiles
from routing_recovery import write_original


@dataclass(frozen=True)
class NativeSchemaDeclaration:
    version: int
    runtime: dict[str, str]
    runtime_digest: str
    binding: dict[str, str]
    binding_digest: str
    coordination_version: int
    snapshot_version: int
    response_version: int
    coordination: dict[str, str]
    writable_columns: dict[str, tuple[str, ...]]
    metadata_rows: dict[str, tuple[tuple[Any, ...], ...]]

    @classmethod
    def observe(cls):
        from agent_comms.coordinated_runtime_schema import _schema, _digest
        from agent_comms.native_runtime_input import NativeRuntimeSchemaMeta
        from agent_comms.native_prompt_binding import PromptBinding
        from agent_comms.private_sidecar import _schema as binding_schema, _digest as binding_digest
        from typing import get_args, get_type_hints
        from agent_comms.coordination_schema import coordinator_schema, CoordinatorTable
        from agent_comms.coordination_tables.metadata import SchemaMeta
        from agent_comms.coordination_response import ResponseTable, ResponseSchemaMeta, _response_schema, _response_digest
        from agent_comms.native_runtime_input import NativeRuntimeTable
        from agent_comms.typed_table import TypedTable
        coordination_meta = SchemaMeta.current()
        response_version = get_args(get_type_hints(ResponseSchemaMeta)['version'])[0]
        native_version = get_args(get_type_hints(NativeRuntimeSchemaMeta)['version'])[0]
        runtime = _schema()
        native_meta = NativeRuntimeSchemaMeta(singleton=1, version=native_version,
                                              ddl_digest=_digest(runtime))
        response = _response_schema()
        # Source-only declaration capture: no product store or original data is
        # opened. SQLite supplies the same normalized DDL spelling as originals.
        with closing(sqlite3.connect(':memory:')) as shape:
            shape.executescript(coordinator_schema())
            coordination_meta.insert(shape)
            for sql in response.values():
                shape.execute(sql)
            ResponseSchemaMeta(1, response_version, _response_digest(response)).insert(shape)
            coordination = objects(shape)
            metadata = {name: tuple(rows(shape, name)) for name in
                        (SchemaMeta.declared_name, ResponseSchemaMeta.declared_name)}
        # Both original4 and current5 declare this metadata row. Source capture
        # must not require a target-only creation method in the original writer.
        metadata[NativeRuntimeSchemaMeta.declared_name] = (
            tuple(getattr(native_meta, name) for name in native_meta.columns()),)
        writable = {table.declared_name: tuple(item.name for item in table._fields()
                    if item.column.generated is None)
                    for family in (CoordinatorTable, ResponseTable, NativeRuntimeTable)
                    for table in TypedTable.members_with(family)}
        return cls(native_version, runtime, _digest(runtime), binding_schema(PromptBinding), binding_digest(PromptBinding),
                   coordination_meta.schema_version, coordination_meta.snapshot_version, response_version,
                   coordination, writable, metadata)

    @property
    def release_versions(self):
        return self.coordination_version, self.snapshot_version, self.response_version, self.version

    @property
    def runtime_objects(self):
        return {**self.coordination, **self.runtime}

    def require_coordination(self, db):
        actual = objects(db)
        if {name: actual.get(name) for name in self.runtime_objects} != self.runtime_objects:
            raise ValueError('Original complete coordinator declaration preimage differs')
        if db.execute('PRAGMA user_version').fetchone()[0] != self.coordination_version:
            raise ValueError('Original coordinator user_version differs')
        for name, expected in self.metadata_rows.items():
            if tuple(rows(db, name)) != expected:
                raise ValueError('Original complete release metadata differs: ' + name)
        self.require_runtime(db)

    def require_runtime(self, db):
        actual = objects(db)
        owned = {name: sql for name, sql in actual.items()
                 if name.startswith(('native_runtime_', 'current_native_cursor'))}
        if owned != self.runtime:
            raise ValueError('Original native declaration preimage differs')
        if rows(db, 'native_runtime_schema_meta') != [(1, self.version, self.runtime_digest)]:
            raise ValueError('Original native schema metadata differs')

    def require_binding(self, db):
        if objects(db) != self.binding or rows(db, 'snapshot_meta') != [(1, self.binding_digest)]:
            raise ValueError('Original prompt binding declaration preimage differs')


class RuntimeNativeFiles(RuntimeCompactionFiles):
    @property
    def paths(self):
        return tuple(self.root / (name + suffix)
                     for name in ('coordination.sqlite3', 'native_prompt_bindings.sqlite3')
                     for suffix in ('', '-journal', '-wal', '-shm'))

    def acquire(self):
        # Parent owns original all-stopped wire exclusion. A hot journal or WAL
        # needs its original operation reviewed, not guessed checkpoint/recovery.
        if any(path.exists() or path.is_symlink() for path in self.paths
               if path.name.endswith(('-journal', '-wal', '-shm'))):
            raise ValueError('Native store companion requires original stopped recovery review')
        for name in ('coordination.sqlite3', 'native_prompt_bindings.sqlite3'):
            marker = self.root / ('.' + name + '.pending')
            if marker.exists() or marker.is_symlink():
                raise ValueError('Original snapshot commit is UNKNOWN; no automatic carry')
        return super().acquire()


def quoted(name):
    from agent_comms.typed_table import _identifier
    return _identifier(name)


def objects(db):
    return dict(db.execute('SELECT name,sql FROM sqlite_master WHERE sql IS NOT NULL'))


def rows(db, table, columns=None):
    selected = '*' if columns is None else ','.join(map(quoted, columns))
    return sorted(db.execute(f'SELECT {selected} FROM {quoted(table)}').fetchall(), key=repr)


def inventory(db):
    names = [row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
    return {name: rows(db, name) for name in names}


def row_digest(data):
    # SQL cell storage, not model/prompt content or another runtime proof store.
    return hashlib.sha256(repr(data).encode()).hexdigest()


def columns(db, table):
    return tuple(row[1] for row in db.execute(f'PRAGMA table_xinfo({quoted(table)})'))


def projection(db, table, target_columns, removed):
    original = columns(db, table)
    if tuple(item for item in original if item not in removed) != target_columns:
        raise ValueError('Reviewed carry does not match the exact removed columns')
    return rows(db, table, target_columns)


def rebuild(db, schema, table_rows):
    # Only cloned databases enter this transaction. Triggers/indices come from
    # authentic declarations, not a second hand-maintained schema registry.
    existing = objects(db)
    changed = set(schema).intersection(existing)
    for name in changed:
        kind = db.execute('SELECT type FROM sqlite_master WHERE name=?', (name,)).fetchone()[0]
        if kind != 'table':
            db.execute(f'DROP {kind.upper()} {quoted(name)}')
    for table in table_rows:
        if table in existing:
            db.execute(f'DROP TABLE {quoted(table)}')
    for name, sql in schema.items():
        if sql.lstrip().startswith('CREATE TABLE') and name in table_rows:
            db.execute(sql)
    for table, (fields, values) in table_rows.items():
        placeholders = ','.join('?' for _ in fields)
        db.executemany(f'INSERT INTO {quoted(table)} ({",".join(map(quoted, fields))}) VALUES ({placeholders})', values)
    for name, sql in schema.items():
        if not sql.lstrip().startswith('CREATE TABLE') and name != 'sqlite_sequence':
            db.execute(sql)
    # SQLite also drops auxiliary triggers/indices attached to a rebuilt table.
    # Their existing owners (for example CohortDeliveryReceipts) are outside
    # this carry. Restore only disappeared objects from the authenticated
    # original preimage; the caller requires exact external DDL/row equality.
    remaining = objects(db)
    for name, sql in existing.items():
        if name not in schema and name not in remaining:
            if sql.lstrip().startswith('CREATE TABLE'):
                raise ValueError('Carry removed an unrelated original table: ' + name)
            db.execute(sql)


def carry_coordination(db, original, target):
    original.require_coordination(db)
    if db.execute('PRAGMA foreign_key_check').fetchall():
        raise ValueError('Original coordinator relations require their owning review')
    before_objects, before_rows = objects(db), inventory(db)
    old_inputs = [dict(zip(columns(db, 'native_runtime_input'), row)) for row in rows(db, 'native_runtime_input')]
    from agent_comms.native_input_record import NativeInputExecution
    from agent_comms.coordination_tables.assignments import WakeAssignment
    if any(name.startswith('selected_native_sources') for name in before_objects):
        raise ValueError('Intermediate batch4 source attestation requires its owning carry review')
    captured = []
    for item in old_inputs:
        assignment = db.execute('SELECT recipient_lookup FROM wake_claims WHERE assignment_id=?', (item['assignment_id'],)).fetchone()
        if assignment != (item['owner_lookup'],):
            raise ValueError('Original native anchor has no matching owner claim')
        execution = NativeInputExecution.decode(item['stage']).from_columns(item['execution_id'], item['attempt_ordinal'])
        # Original4 NULL execution is the declared TRIAGE association. FULL
        # already owns its immutable execution links; retain those exact links,
        # not an expanded cohort inferred from message content or native history.
        source_ids = ((item['assignment_id'],) if item['execution_id'] is None else
                      tuple(row[0] for row in db.execute('SELECT assignment_id FROM execution_claims WHERE execution_id=? ORDER BY ordinal', (item['execution_id'],))))
        if item['assignment_id'] not in source_ids:
            raise ValueError('Original native anchor is absent from original execution links')
        assignments = tuple(WakeAssignment.one(db, assignment_id=source_id) for source_id in source_ids)
        if any(assignment is None or assignment.recipient_lookup != item['owner_lookup'] for assignment in assignments):
            raise ValueError('Original native membership crosses its recorded owner')
        captured.append((item, execution, assignments))
    # A malformed original cursor cannot be repaired by dropping its old anchor.
    for cursor in db.execute('SELECT input_id,assignment_id FROM current_native_cursor WHERE input_id IS NOT NULL'):
        anchor = next((item['assignment_id'] for item in old_inputs if item['input_id'] == cursor[0]), None)
        if cursor[1] != anchor:
            raise ValueError('Original cursor anchor differs from its physical native input')
    # The original singleton obligation is the sole historical route authority.
    # No route is added from other claim bodies, current routing or new batching.
    obligations = {row['execution_id']: row for row in
                   (dict(zip(columns(db, 'obligations'), value))
                    for value in rows(db, 'obligations'))}
    executions = {row['execution_id']: row for row in
                  (dict(zip(columns(db, 'executions'), value))
                   for value in rows(db, 'executions'))}
    for execution_id, execution in executions.items():
        obligation = obligations.get(execution_id)
        if execution['origin'] == 'wire':
            if obligation is None or obligation['exact_target'] != execution['exact_target']:
                raise ValueError('Original scalar execution differs from its original obligation')
        elif obligation is not None or execution['exact_target'] is not None:
            raise ValueError('Original claimless execution has a response route')
    intents = {row['execution_id']: row for row in
               (dict(zip(columns(db, 'publication_intents'), value))
                for value in rows(db, 'publication_intents'))}
    for execution_id, intent in intents.items():
        if execution_id not in obligations or intent['exact_target'] != obligations[execution_id]['exact_target']:
            raise ValueError('Original intent differs from its original response route')

    removed_writable = {'executions': {'exact_target'},
                        'native_runtime_input': {'assignment_id'},
                        'current_native_cursor': {'assignment_id'}}
    added_writable = {'publication_receipts': {'exact_target'},
                      'publication_append_dispatches': {'exact_target'}}
    payload = {}
    for name, fields in target.writable_columns.items():
        if name in target.metadata_rows:
            payload[name] = (fields, list(target.metadata_rows[name]))
            continue
        if name not in before_rows:
            if name != 'native_runtime_triage_sources':
                raise ValueError('Unreviewed new release table: ' + name)
            payload[name] = (fields, [])
            continue
        old_fields = original.writable_columns[name]
        if (set(old_fields) - set(fields) != removed_writable.get(name, set())
                or set(fields) - set(old_fields) != added_writable.get(name, set())):
            raise ValueError('Unreviewed writable column change: ' + name)
        values = []
        for value in rows(db, name, old_fields):
            record = dict(zip(old_fields, value))
            if name in added_writable:
                intent = intents.get(record['execution_id'])
                if intent is None:
                    raise ValueError('Original publication evidence lacks its frozen intent')
                record['exact_target'] = intent['exact_target']
            values.append(tuple(record[field] for field in fields))
        payload[name] = (fields, values)
    # Rebuild one matched clone from ALL existing declaration families. Generated
    # columns, route FKs, guards and retry view are derived, not copied decisions.
    sequence = rows(db, 'sqlite_sequence') if 'sqlite_sequence' in before_objects else None
    rebuild(db, target.runtime_objects, payload)
    if sequence is not None:
        db.execute('DELETE FROM sqlite_sequence')
        db.executemany('INSERT INTO sqlite_sequence(name,seq) VALUES (?,?)', sequence)
        if rows(db, 'sqlite_sequence') != sequence:
            raise ValueError('Original sequence counters changed')
    db.execute(f'PRAGMA user_version={target.coordination_version}')
    for item, execution, assignments in captured:
        # The existing family validates the exact retained execution links or
        # records TRIAGE's original singleton. No native proof is reconstructed.
        execution.record_sources(db, item['input_id'], assignments)
    payload['native_runtime_triage_sources'] = (payload['native_runtime_triage_sources'][0], rows(db, 'native_runtime_triage_sources'))
    target.require_coordination(db)
    after_objects, after_rows = objects(db), inventory(db)
    # An existing capability entering the target family must match its original
    # definition exactly; target membership cannot authorize an unrelated edit.
    inherited = (before_objects.keys() & target.runtime_objects.keys()) - original.runtime_objects.keys()
    if any(before_objects[name] != target.runtime_objects[name] for name in inherited):
        raise ValueError('Target declaration changes an unrelated existing capability')
    owned_names = original.runtime_objects.keys() | target.runtime_objects.keys()
    outside_before = {k:v for k,v in before_objects.items() if k not in owned_names}
    outside_after = {k:v for k,v in after_objects.items() if k not in owned_names}
    if outside_before != outside_after:
        changed = sorted(name for name in outside_before.keys() | outside_after.keys()
                         if outside_before.get(name) != outside_after.get(name))
        raise ValueError('Carry changed unrelated coordination schema: ' + ', '.join(changed))
    unchanged_tables = {name for name in before_rows if name not in target.metadata_rows
                        and name not in removed_writable and name not in added_writable}
    untouched = unchanged_tables
    if any(after_rows[name] != before_rows[name] for name in untouched):
        raise ValueError('Carry changed unrelated original coordination rows')
    for name, (fields, expected) in payload.items():
        if rows(db, name, fields) != sorted(expected, key=repr):
            raise ValueError('Carried native rows differ from exact original projection')
    if db.execute('PRAGMA foreign_key_check').fetchall():
        raise ValueError('Carried native relations violate foreign keys')
    return {'original_release': list(original.release_versions), 'target_release': list(target.release_versions),
            'historical_routes': {key: value['exact_target'] for key, value in obligations.items()},
            'original_rows':{k:len(v) for k,v in before_rows.items()}, 'carried_rows':{k:len(v) for k,v in after_rows.items()},
            'unchanged_rows_sha256':row_digest({k:before_rows[k] for k in sorted(untouched)}),
            'native_projection_sha256':row_digest(payload), 'triage_originals':len(payload['native_runtime_triage_sources'][1])}


def carry_binding(db, original, target, sources):
    original.require_binding(db)
    original_columns = columns(db, 'prompt_binding')
    identity = ('assignment_id','stage','execution_id','attempt_ordinal','owner_lookup','owner_thread','owner_generation')
    for value in rows(db, 'prompt_binding'):
        binding = dict(zip(original_columns, value))
        source = sources.get(binding['input_id'])
        if source is None or any(binding[key] != source[key] for key in identity):
            raise ValueError('Original prompt binding does not name its exact reserved native input')
        if (binding['source_seq'], binding['message_id']) != (source['wire_seq'], source['message_id']):
            raise ValueError('Original prompt binding names another original source')
    with closing(sqlite3.connect(':memory:')) as target_shape:
        for sql in target.binding.values():
            target_shape.execute(sql)
        fields = columns(target_shape, 'prompt_binding')
    expected = projection(db, 'prompt_binding', fields, {'assignment_id', 'source_seq', 'message_id'})
    payload = {'prompt_binding':(fields, expected), 'snapshot_meta':(('singleton','ddl_digest'), [(1,target.binding_digest)])}
    rebuild(db, target.binding, payload)
    target.require_binding(db)
    if rows(db, 'prompt_binding', fields) != expected:
        raise ValueError('Carry changed original prompt binding evidence')
    return {'original_bindings':len(expected), 'remaining_columns_sha256':row_digest(expected)}


@dataclass(frozen=True)
class CarriedNativeStore:
    name: str
    original_sha256: str
    candidate_sha256: str
    evidence: dict[str, Any]


@dataclass(frozen=True)
class NativeSchemaCarryPlan:
    root: Annotated[Path, PathText]
    candidate: Annotated[Path, PathText]
    original: NativeSchemaDeclaration
    target: NativeSchemaDeclaration
    stores: tuple[CarriedNativeStore, ...]

    def require_candidate(self):
        if self.original.release_versions != (8, 2, 2, 4) or self.target.release_versions != (9, 3, 3, 5):
            raise ValueError('Only reviewed complete490 release8/2/2/4->9/3/3/5 carry is authorized')
        if NativeSchemaDeclaration.observe() != self.target:
            raise ValueError('Matched target declarations changed after carry preparation')
        if tuple(item.name for item in self.stores) not in (('coordination.sqlite3',), ('coordination.sqlite3','native_prompt_bindings.sqlite3')):
            raise ValueError('Carry must cover exactly the original native stores')
        PrivateDirectoryRole.require(self.candidate.lstat())
        with RuntimeNativeFiles(self.candidate).acquire() as acquired:
            observed = {item.path.name:item.sha256 for item in acquired.originals}
            if observed != {item.name:item.candidate_sha256 for item in self.stores}:
                raise ValueError('Reviewed native candidate changed')

    def install(self, destination):
        self.require_candidate()
        from agent_comms.private_sidecar import _locked_directory, _read_snapshot, _publish
        with ExitStack() as custody:
            snapshots = {}
            for item in self.stores:
                directory = custody.enter_context(_locked_directory(self.root / item.name))
                snapshot = _read_snapshot(directory, item.name)
                if snapshot is None:
                    raise ValueError('Original carry store disappeared: ' + item.name)
                snapshots[item.name] = (directory, snapshot[1])
            acquired = custody.enter_context(RuntimeNativeFiles(self.root).acquire())
            by_name = {item.path.name:item for item in acquired.originals}
            if set(by_name) != {item.name for item in self.stores}:
                raise ValueError('Original native store membership changed')
            if any(by_name[item.name].sha256 != item.original_sha256 for item in self.stores):
                raise ValueError('Original native preimage changed after review')
            destination.mkdir(mode=0o700)
            for item in self.stores:
                retain_file(self.root / item.name, destination / item.name)
            write_original(destination / 'reviewed-carry.json', (json.dumps(FieldCodec.encode(self), indent=2)+'\n').encode())
            fsync_directory(destination)
            fsync_directory(destination.parent)
            acquired.require_original()
            # All preimages and both candidates exist before the first replace.
            # Any exception leaves the existing stopped batch and this attempt
            # directory intact. No implicit rollback, launch or retry.
            for item in self.stores:
                staging = destination / (item.name + '.target')
                retain_file(self.candidate / item.name, staging)
                if digest(staging) != item.candidate_sha256:
                    raise ValueError('Staged candidate differs from reviewed carry')
            acquired.require_original()
            for item in self.stores:
                staging = destination / (item.name + '.target')
                # The retained candidate/preimages may live on another mount.
                # Existing binary publication creates/fsyncs its staging in the
                # pinned ROOT directory and atomically replaces there. Its
                # UNKNOWN intent remains on failure; never replay or repair it.
                directory, identity = snapshots[item.name]
                _publish(directory, item.name, identity, staging.read_bytes())
                staging.unlink()
            if any(digest(self.root / item.name) != item.candidate_sha256 for item in self.stores):
                raise ValueError('Installed native carry differs; remain stopped')
            result = {'classification':'runtime/complete490-release-preserve', 'stores':FieldCodec.encode(self.stores),
                      'original_preimages':str(destination), 'retired':[], 'reconstructed_proofs':0, 'input_replays':0}
            write_original(destination / 'installed.json', (json.dumps(result,indent=2)+'\n').encode())
            fsync_directory(destination)
            return result


def prepare(root, candidate, original):
    target = NativeSchemaDeclaration.observe()
    if original.release_versions != (8, 2, 2, 4) or target.release_versions != (9, 3, 3, 5):
        raise ValueError('Original8/2/2/4 and matched target9/3/3/5 declarations are required')
    root, candidate = root.absolute(), candidate.absolute()
    if candidate == root or candidate.is_relative_to(root):
        raise ValueError('Candidate must be separate persistent owned storage')
    candidate.mkdir(mode=0o700)
    stores = []
    with RuntimeNativeFiles(root).acquire() as acquired:
        if not any(item.path.name == 'coordination.sqlite3' for item in acquired.originals):
            raise ValueError('No original native coordinator to carry')
        for item in acquired.originals:
            retain_file(item.path, candidate / item.path.name)
        with closing(sqlite3.connect((candidate / 'coordination.sqlite3').as_uri()+'?mode=ro', uri=True)) as snapshot:
            sources = {row[0]:dict(zip(columns(snapshot,'native_runtime_input'),row)) for row in rows(snapshot,'native_runtime_input')}
            for source in sources.values():
                claim = snapshot.execute('SELECT wire_seq,message_id FROM wake_claims WHERE assignment_id=?', (source['assignment_id'],)).fetchone()
                if claim is None:
                    raise ValueError('Original native source lacks its claim')
                source.update(wire_seq=claim[0], message_id=claim[1])
        for item in acquired.originals:
            path = candidate / item.path.name
            with closing(sqlite3.connect(path, isolation_level=None)) as db:
                db.execute('PRAGMA foreign_keys=OFF')
                db.execute('PRAGMA synchronous=FULL')
                db.execute('BEGIN IMMEDIATE')
                try:
                    evidence = (carry_coordination(db, original, target) if item.path.name == 'coordination.sqlite3'
                                else carry_binding(db, original, target, sources))
                    db.execute('COMMIT')
                except BaseException:
                    db.execute('ROLLBACK')
                    raise
            with path.open('rb') as saved:
                os.fsync(saved.fileno())
            stores.append(CarriedNativeStore(item.path.name, item.sha256, digest(path), evidence))
        acquired.require_original()
    fsync_directory(candidate)
    plan = NativeSchemaCarryPlan(root, candidate, original, target, tuple(stores))
    plan.require_candidate()
    return plan


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--declaration', action='store_true')
    parser.add_argument('--root', type=Path)
    parser.add_argument('--candidate', type=Path)
    parser.add_argument('--original-declaration', type=Path)
    parser.add_argument('--plan', type=Path)
    args = parser.parse_args()
    if args.declaration:
        print(json.dumps(FieldCodec.encode(NativeSchemaDeclaration.observe())))
    else:
        if not all((args.root,args.candidate,args.original_declaration,args.plan)):
            parser.error('Preparation requires root, candidate, original declaration and fresh plan')
        original = FieldCodec.decode(NativeSchemaDeclaration,json.loads(args.original_declaration.read_text()))
        plan = prepare(args.root,args.candidate,original)
        write_original(args.plan,(json.dumps(FieldCodec.encode(plan),indent=2)+'\n').encode())
        print(json.dumps({'plan':str(args.plan),'stores':len(plan.stores),'source_unchanged':True}))
