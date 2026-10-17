"""One-use declared native carry under the existing stopped custody.

Original declarations authenticate the source. Current declarations own target
DDL. Only matched clones are transformed; original files remain preimages.
"""
from __future__ import annotations

from abc import abstractmethod
from contextlib import ExitStack, closing
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
from typing import Annotated, Any, ClassVar

from agent_comms.field_codec import FieldCodec, PathText
from agent_comms.declared_family import DeclaredFamily
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
    goal: dict[str, str]
    writable_columns: dict[str, tuple[str, ...]]
    metadata_rows: dict[str, tuple[tuple[Any, ...], ...]]
    compaction: dict[str, str]
    compaction_columns: dict[str, tuple[str, ...]]

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
        from agent_comms.compaction_records import JournalTable
        # The original interpreter need not expose a target-only schema helper.
        # Both authentic producers declare membership through this capability.
        goal = GoalNativeStore.schema()
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
        # Both original5 and current6 declare this metadata row. Source capture
        # must not require a target-only creation method in the original writer.
        metadata[NativeRuntimeSchemaMeta.declared_name] = (
            tuple(getattr(native_meta, name) for name in native_meta.columns()),)
        writable = {table.declared_name: tuple(item.name for item in table._fields()
                    if item.column.generated is None)
                    for family in (CoordinatorTable, ResponseTable, NativeRuntimeTable)
                    for table in TypedTable.members_with(family)}
        journals = tuple(TypedTable.members_with(JournalTable))
        with closing(sqlite3.connect(':memory:')) as shape:
            for table in journals:
                table.create(shape)
            compaction = objects(shape)
        return cls(native_version, runtime, _digest(runtime), binding_schema(PromptBinding), binding_digest(PromptBinding),
                   coordination_meta.schema_version, coordination_meta.snapshot_version, response_version,
                   coordination, goal, writable, metadata, compaction,
                   {table.declared_name: table.columns() for table in journals})

    @property
    def release_versions(self):
        return self.coordination_version, self.snapshot_version, self.response_version, self.version

    def require_carry_target(self, target):
        """Authenticate the reviewed conversion from original declarations.

        A same-release declaration change cannot reinterpret any original fact.
        New declaration members start empty; only their original producer can
        create annotations, enrollment, native coverage or fork evidence later.
        """
        if self.release_versions == (9, 3, 3, 5) and target.release_versions == (9, 3, 3, 6):
            return
        metadata = dict(self.metadata_rows)
        if self.release_versions == (9, 3, 3, 6) and target.release_versions == (10, 3, 3, 6):
            from agent_comms.coordination_tables.metadata import SchemaMeta

            if (not self.coordination.items() <= target.coordination.items()
                    or not self.writable_columns.items() <= target.writable_columns.items()):
                raise ValueError('Coordination carry cannot reinterpret original declarations')
            # The existing metadata declaration owns the release marker. No
            # fact row, native proof or response schema gets a new meaning.
            metadata[SchemaMeta.declared_name] = target.metadata_rows[SchemaMeta.declared_name]
        elif (self.release_versions != target.release_versions
              or target.release_versions not in ((9, 3, 3, 6), (10, 3, 3, 6))):
            raise ValueError('Unreviewed native or coordination release conversion')
        elif (self.coordination != target.coordination
              or self.writable_columns != target.writable_columns):
            raise ValueError('Same-release carry cannot change original coordination declarations')
        if (self.runtime != target.runtime or self.runtime_digest != target.runtime_digest
                or self.binding != target.binding or self.binding_digest != target.binding_digest
                or metadata != target.metadata_rows):
            raise ValueError('Carry cannot reinterpret original native or response declarations')
        if (not self.compaction_columns.items() <= target.compaction_columns.items()
                or not self.compaction.items() <= target.compaction.items()):
            raise ValueError('Additive journal carry cannot reinterpret original declarations')

    def capture_requests(self, path, source_python, original):
        original.require_carry_target(self)
        if original.version == self.version:
            return []
        return capture_requests(path, source_python, original)

    def empty_journal_members(self, original):
        if not original.compaction_columns.keys() <= self.compaction_columns.keys():
            raise ValueError('Carry cannot discard original journal members')
        return {name: (self.compaction_columns[name], [])
                for name in self.compaction_columns.keys() - original.compaction_columns.keys()}

    def carry_journal_members(self, db, original):
        """Create declaration-owned empty members without rewriting old rows."""
        from agent_comms.compaction_records import JournalTable
        from agent_comms.typed_table import TypedTable

        original.require_carry_target(self)
        original.require_compaction(db)
        if db.execute('PRAGMA foreign_key_check').fetchall():
            raise ValueError('Original journal relations require their owning review')
        tables = {table.declared_name: table for table in TypedTable.members_with(JournalTable)}
        identities = {
            name: rows(db, name, fields if tables[name].without_rowid else ('rowid', *fields))
            for name, fields in original.compaction_columns.items()
        }
        additions = self.empty_journal_members(original)
        rebuild(db, self.compaction, additions)
        self.require_compaction(db)
        for name, before in identities.items():
            fields = original.compaction_columns[name]
            if rows(db, name, fields if tables[name].without_rowid else ('rowid', *fields)) != before:
                raise ValueError('Additive journal carry changed original row identities or facts')
        if any(tables[name].select(db) for name in additions):
            raise ValueError('New journal members cannot contain inferred historical evidence')
        if db.execute('PRAGMA foreign_key_check').fetchall():
            raise ValueError('Carried journal relations violate foreign keys')
        return {'created_empty_tables': sorted(additions),
                'original_identity_rows_sha256': row_digest(identities),
                'original_rows': {name: len(values) for name, values in identities.items()},
                'original_source_bytes_preserved': True,
                'admission_receipts_minted': 0, 'input_replays': 0}

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

    def require_compaction(self, db):
        if objects(db) != self.compaction:
            raise ValueError('Original compaction journal declaration preimage differs')


class RuntimeNativeFiles(RuntimeCompactionFiles):
    @property
    def paths(self):
        return tuple(self.root / (name + suffix)
                     for name in (store.name for store in CarriedNativeStore.members_with(NativeReleaseStore))
                     for suffix in ('', '-journal', '-wal', '-shm'))

    def acquire(self):
        # Parent owns original all-stopped wire exclusion. A hot journal or WAL
        # needs its original operation reviewed, not guessed checkpoint/recovery.
        if any(path.exists() or path.is_symlink() for path in self.paths
               if path.name.endswith(('-journal', '-wal', '-shm'))):
            raise ValueError('Native store companion requires original stopped recovery review')
        for name in (CoordinationNativeStore.name, PromptBindingNativeStore.name):
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
    return sorted((tuple(row) for row in db.execute(f'SELECT {selected} FROM {quoted(table)}')), key=repr)


def inventory(db):
    names = [row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
    return {name: rows(db, name) for name in names}


def row_digest(data):
    # SQL cell storage, not model/prompt content or another runtime proof store.
    return hashlib.sha256(repr(data).encode()).hexdigest()


def columns(db, table):
    return tuple(row[1] for row in db.execute(f'PRAGMA table_xinfo({quoted(table)})'))


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


def carry_goal(db):
    """Rebuild generated constraints as one family, preserving every fact row."""
    from agent_comms.goal_attempts import GoalAttemptSchema, GoalLedgerTable
    from agent_comms.typed_table import TypedTable
    before_objects, before_rows = objects(db), inventory(db)
    if db.execute('PRAGMA integrity_check').fetchall() != [('ok',)]:
        raise ValueError('Original goal ledger integrity is uncertain')
    if db.execute('PRAGMA foreign_key_check').fetchall():
        raise ValueError('Original goal ledger relations require their owning review')
    tables = {table.declared_name: table for table in TypedTable.members_with(GoalLedgerTable)}
    if not before_rows.keys() <= tables.keys():
        raise ValueError('Carry cannot discard an original goal table')
    payload, identity_rows = {}, {}
    for name, table in tables.items():
        fields = tuple(item.name for item in table._fields() if item.column.generated is None)
        if table is GoalAttemptSchema:
            if len(before_rows.get(name, ())) != 1:
                raise ValueError('Original goal declaration marker is uncertain')
            current = GoalAttemptSchema.current()
            payload[name] = (fields, [tuple(getattr(current, field) for field in fields)])
            continue
        if name not in before_rows:
            payload[name] = (fields, [])
            continue
        old_fields = tuple(row[1] for row in db.execute(f'PRAGMA table_xinfo({quoted(name)})')
                           if row[6] == 0)
        if old_fields != fields:
            raise ValueError('Goal fact columns need an explicit semantic carry: ' + name)
        identities = fields if table.without_rowid else ('rowid', *fields)
        identity_rows[name] = rows(db, name, identities)
        payload[name] = (identities, identity_rows[name])
    target = GoalNativeStore.schema()
    rebuild(db, target, payload)
    GoalNativeStore.require_schema(db)
    after_rows = inventory(db)
    for name in before_rows.keys() - {GoalAttemptSchema.declared_name}:
        if after_rows[name] != before_rows[name]:
            raise ValueError('Goal carry changed an original fact or generated fact: ' + name)
        if rows(db, name, payload[name][0]) != identity_rows[name]:
            raise ValueError('Goal carry changed an original physical row identity: ' + name)
    if db.execute('PRAGMA integrity_check').fetchall() != [('ok',)] or db.execute('PRAGMA foreign_key_check').fetchall():
        raise ValueError('Carried goal ledger constraints are not satisfied')
    return {'original_ddl_sha256': row_digest(before_objects),
            'target_ddl_sha256': row_digest(target),
            'original_rows': {name: len(values) for name, values in before_rows.items()},
            'carried_rows': {name: len(values) for name, values in after_rows.items()},
            'unchanged_rows_sha256': row_digest({name: before_rows[name] for name in sorted(identity_rows)}),
            'row_identity_sha256': row_digest(identity_rows),
            'changed_schema_objects': sorted(name for name in before_objects.keys() | target.keys()
                                             if before_objects.get(name) != target.get(name)),
            'metadata_owner': GoalAttemptSchema.declared_name}


def carry_coordination(db, original, target):
    """Carry declaration members; existing facts keep their original identities."""
    from agent_comms.coordination_schema import CoordinatorTable
    from agent_comms.coordination_response import ResponseTable
    from agent_comms.native_runtime_input import NativeRuntimeTable
    from agent_comms.typed_table import TypedTable

    original.require_carry_target(target)
    original.require_coordination(db)
    if db.execute('PRAGMA foreign_key_check').fetchall():
        raise ValueError('Original coordinator relations require their owning review')
    before_objects, before_rows = objects(db), inventory(db)
    if not original.writable_columns.items() <= target.writable_columns.items():
        raise ValueError('Carry cannot invent or discard original writable facts')
    additions = target.writable_columns.keys() - original.writable_columns.keys()
    if (target.runtime_objects.keys() - original.runtime_objects.keys()) & before_objects.keys():
        raise ValueError('New declaration collides with an unrelated original schema object')
    tables = {table.declared_name: table
              for family in (CoordinatorTable, ResponseTable, NativeRuntimeTable)
              for table in TypedTable.members_with(family)}
    identities, payload = {}, {}
    for name, fields in target.writable_columns.items():
        if name in additions:
            payload[name] = (fields, [])
            continue
        identity_fields = fields if tables[name].without_rowid else ('rowid', *fields)
        values = rows(db, name, identity_fields)
        identities[name] = (identity_fields, values)
        if (original.runtime_objects[name] == target.runtime_objects[name]
                and original.metadata_rows.get(name) == target.metadata_rows.get(name)):
            continue
        if name in target.metadata_rows:
            metadata = target.metadata_rows[name]
            values = (list(metadata) if identity_fields == fields else
                      [(row[0], *value) for row, value in zip(values, metadata, strict=True)])
        payload[name] = (identity_fields, values)
    sequence = rows(db, 'sqlite_sequence') if 'sqlite_sequence' in before_objects else ()
    rebuild(db, target.runtime_objects, payload)
    if 'sqlite_sequence' in objects(db):
        db.execute('DELETE FROM sqlite_sequence')
        db.executemany('INSERT INTO sqlite_sequence(name,seq) VALUES (?,?)', sequence)
    db.execute(f'PRAGMA user_version={target.coordination_version}')
    target.require_coordination(db)
    after_objects, after_rows = objects(db), inventory(db)
    owned = original.runtime_objects.keys() | target.runtime_objects.keys()
    if {k:v for k,v in before_objects.items() if k not in owned} != {
            k:v for k,v in after_objects.items() if k not in owned}:
        raise ValueError('Carry changed unrelated coordination declarations')
    unchanged = {name:values for name,values in before_rows.items()
                 if name not in target.metadata_rows}
    if any(after_rows[name] != values for name,values in unchanged.items()):
        raise ValueError('Carry changed original coordination facts')
    for name, (fields, values) in identities.items():
        expected = payload[name][1] if name in target.metadata_rows and name in payload else values
        if rows(db, name, fields) != expected:
            raise ValueError('Carry changed original row identity: ' + name)
    if after_rows.keys() != before_rows.keys() | additions or any(after_rows[name] for name in additions):
        raise ValueError('New declaration members must remain empty')
    if 'sqlite_sequence' in objects(db) and rows(db, 'sqlite_sequence') != sequence:
        raise ValueError('Carry changed original autoincrement allocations')
    from agent_comms.native_runtime_input import NativeRuntimeInput, CurrentNativeCursor
    # The target declarations reject partial references. Reading cannot create
    # a selected session, proof, cursor, assignment, or new admission.
    NativeRuntimeInput.select(db)
    CurrentNativeCursor.select(db)
    if db.execute('PRAGMA foreign_key_check').fetchall():
        raise ValueError('Carried original native relations violate foreign keys')
    return {'original_release':list(original.release_versions),
            'target_release':list(target.release_versions),
            'original_ddl_sha256':row_digest(before_objects),
            'target_ddl_sha256':row_digest(target.runtime_objects),
            'created_empty_tables':sorted(additions),
            'rebuilt_tables':sorted(payload.keys() - additions),
            'original_identity_rows_sha256':row_digest(identities),
            'autoincrement_rows_sha256':row_digest(sequence),
            'unchanged_rows_sha256':row_digest(unchanged),
            'original_rows':{name:len(values) for name,values in before_rows.items()}}


def carry_binding(db, original, target):
    # Native5 already removed the former assignment/sequence mirrors. Native6
    # neither selects a different native input nor changes its frozen prompt.
    original.require_binding(db)
    target.require_binding(db)
    return {'original_bindings':len(rows(db, 'prompt_binding')),
            'unchanged_rows_sha256':row_digest(inventory(db))}


def original_compaction_requests(path):
    """Run ONLY in the authentic Native5 interpreter, on an acquired clone.

    Its original classes certify meaning. The external one-use transfer adds
    declared references, never an old reader to the current product codec.
    """
    from agent_comms.compaction_records import SelectedSummaryAttempt
    from agent_comms.input_origin import InputProvenance
    from agent_comms.selected_source import SelectedAdmissionSource

    declared = NativeSchemaDeclaration.observe()
    if declared.release_versions != (9, 3, 3, 5):
        raise ValueError('Authentic Native5 producer required for original source decoding')
    result = []
    with closing(sqlite3.connect(path.absolute().as_uri()+'?mode=ro', uri=True)) as db:
        db.execute('PRAGMA query_only=ON')
        db.execute('BEGIN')
        try:
            declared.require_compaction(db)
            for attempt in SelectedSummaryAttempt.select(db, order_by=('operation_id',)):
                envelope = attempt.envelope()
                if envelope.journal_json() != attempt.source_json:
                    raise ValueError('Original source does not round-trip its authentic declaration')
                originals = {}
                for fact in envelope.retained.facts:
                    for ref, row in fact.original_sources():
                        if isinstance(ref, InputProvenance):
                            if ref != row.context_provenance():
                                raise ValueError('Frozen original input provenance differs')
                            if ref.key in originals and originals[ref.key] != row:
                                raise ValueError('Frozen original input evidence is ambiguous')
                            originals[ref.key] = row

                request = FieldCodec.encode(envelope)
                source = envelope.source
                if isinstance(source, SelectedAdmissionSource):
                    row = originals.get(source.ingress_key)
                    if row is None or row.digest != source.original_digest:
                        raise ValueError('Selected original lacks its frozen content witness')
                    data = request['source']
                    del data['ingress_key'], data['original_digest']
                    # Keep the original durable key/origin reference. The
                    # existing retained InputTaskFact/StoredInput owns its
                    # content digest; never copy it into durable provenance.
                    data['originals'] = [FieldCodec.encode(row.context_provenance())]
                result.append({'operation_id':attempt.operation_id,
                               'session_file':attempt.session_file,
                               'source_json_sha256':hashlib.sha256(attempt.source_json.encode()).hexdigest(),
                               'request':request})
        finally:
            db.execute('ROLLBACK')
    import agent_comms
    return {'source_package':agent_comms.__file__, 'declaration':FieldCodec.encode(declared),
            'attempts':result}


def capture_requests(path, source_python, original):
    environment = dict(os.environ)
    environment.pop('PYTHONPATH', None)
    captured = subprocess.run(
        [str(source_python), str(Path(__file__).resolve()), '--compaction-requests', str(path)],
        env=environment, text=True, capture_output=True, timeout=30, check=True)
    packet = json.loads(captured.stdout)
    if FieldCodec.decode(NativeSchemaDeclaration, packet['declaration']) != original:
        raise ValueError('Original source decoder declarations differ from reviewed preimage')
    if not Path(packet['source_package']).is_relative_to(source_python.absolute().parent.parent):
        raise ValueError('Original decoder did not use its installed source package')
    return packet['attempts']


def carry_compaction(db, original, target, requests):
    from agent_comms.compaction_records import SelectedSummaryAttempt, SelectedSummarySource

    original.require_carry_target(target)
    if original.version == target.version:
        if requests:
            raise ValueError('Additive journal carry cannot convert historical requests')
        return target.carry_journal_members(db, original)
    original.require_compaction(db)
    before_rows = inventory(db)
    name = SelectedSummaryAttempt.declared_name
    additions = target.empty_journal_members(original)
    for table, fields in original.compaction_columns.items():
        expected = tuple(f for f in target.compaction_columns[table] if f != 'request') if table == name else target.compaction_columns[table]
        if fields != expected:
            raise ValueError('Unreviewed compaction journal field change: '+table)
    prior = rows(db, name, ('rowid', *original.compaction_columns[name]))
    by_identity = {item['operation_id']:item for item in requests}
    if len(by_identity) != len(requests) or set(by_identity) != {row[1] for row in prior}:
        raise ValueError('Original request inventory differs from exact journal membership')
    fields = SelectedSummaryAttempt._fields()
    request_field = next(item for item in fields if item.name == 'request')
    values = []
    for rowid, operation_id, session_file, source_json, state in prior:
        item = by_identity[operation_id]
        if item['session_file'] != session_file or item['source_json_sha256'] != hashlib.sha256(source_json.encode()).hexdigest():
            raise ValueError('Original source evidence changed after authentic decoding')
        request = FieldCodec.decode(SelectedSummarySource, item['request'])
        request.journal_json()  # Existing retained-payload/control bound.
        values.append((rowid, operation_id, session_file, source_json,
                       request_field.encode(request), state))
    rebuild(db, target.compaction, {name:(('rowid', *target.compaction_columns[name]), values),
                                    **additions})
    target.require_compaction(db)
    after_rows = inventory(db)
    untouched = {table:data for table,data in before_rows.items() if table != name}
    if any(after_rows[table] != data for table,data in untouched.items()):
        raise ValueError('Carry changed original enrollment/UNKNOWN/native evidence')
    if rows(db, name, ('rowid', *original.compaction_columns[name])) != prior:
        raise ValueError('Carry changed historical order, source bytes or attempt disposition')
    # Strict current declarations validate every request. Native references
    # still verify the untouched original byte string, never its new encoding.
    for attempt in SelectedSummaryAttempt.select(db):
        for intent_json, in db.execute('SELECT intent_json FROM operations WHERE session_file=?', (attempt.session_file,)):
            from agent_comms.compaction_identity import SelectedCommitReference
            intent = json.loads(intent_json)
            if intent.get('selectedSummaryOperationId') == attempt.operation_id:
                SelectedCommitReference.from_intent(intent).require_source(attempt.source_json)
    if db.execute('PRAGMA foreign_key_check').fetchall():
        raise ValueError('Carry changed original journal references')
    if any(rows(db, table) for table in additions):
        raise ValueError('Added journal declarations acquired invented historical evidence')
    return {'attempts':len(values), 'created_empty_tables':sorted(additions),
            'original_selected_rows_sha256':row_digest(prior),
            'unchanged_tables_sha256':row_digest(untouched),
            'original_source_bytes_preserved':True, 'admission_receipts_minted':0,
            'input_replays':0}


@dataclass(frozen=True)
class CarriedNativeStore(DeclaredFamily, affix='NativeStore'):
    """Each original file owns its carry and physical publication behavior."""

    name: ClassVar[str]
    original_sha256: str
    candidate_sha256: str
    evidence: dict[str, Any]

    def publisher(self, root, custody):
        def publish(staging, original):
            staging.replace_original(original)
        return publish


class NativeReleaseStore(CarriedNativeStore):
    """A native release transforms source/target records and retained requests."""

    @classmethod
    @abstractmethod
    def carry(cls, db, original, target, requests): ...


class GoalNativeStore(CarriedNativeStore):
    name = 'goal-private/goal_attempts.sqlite3'

    @staticmethod
    def schema():
        from agent_comms.goal_attempts import GoalLedgerTable
        from agent_comms.typed_table import TypedTable
        return {name: sql for table in TypedTable.members_with(GoalLedgerTable)
                for name, sql in table.schema_objects().items()}

    @classmethod
    def require_schema(cls, db):
        from agent_comms.goal_attempts import assert_goal_attempt_schema
        if objects(db) != cls.schema():
            raise ValueError('Goal ledger differs from the target declaration')
        assert_goal_attempt_schema(db)

    @classmethod
    def synchronize(cls, acquired, destination, original_schema):
        """Goal DDL does not depend on retired native compaction declarations."""
        acquired.require_original()
        if not acquired.originals:
            return {'classification': 'goal/absent', 'created': False}
        original, = acquired.originals
        schema = cls.schema()
        with closing(sqlite3.connect(original.path.as_uri()+'?mode=ro', uri=True)) as db:
            if objects(db) == schema:
                cls.require_schema(db)
                return {'classification': 'goal/preserve', 'original_files': acquired.evidence()}
            if objects(db) != original_schema:
                raise ValueError('Original goal ledger differs from its authentic declaration preimage')
        candidate = destination.with_name(destination.name + '.candidate')
        candidate.mkdir(mode=0o700)
        name = str(original.path.relative_to(acquired.paths[0].parents[1]))
        if name != cls.name:
            raise ValueError('Acquired goal resource names another store')
        path = candidate / name
        path.parent.mkdir(mode=0o700)
        retain_file(original.path, path)
        with closing(sqlite3.connect(path, isolation_level=None)) as db:
            db.execute('PRAGMA foreign_keys=OFF')
            db.execute('PRAGMA synchronous=EXTRA')
            db.execute('BEGIN IMMEDIATE')
            try:
                evidence = carry_goal(db)
                db.execute('COMMIT')
            except BaseException:
                db.execute('ROLLBACK')
                raise
        with path.open('rb') as stream:
            os.fsync(stream.fileno())
        fsync_directory(path.parent)
        fsync_directory(candidate)
        store = cls(original.sha256, digest(path), evidence)
        result = NativeSchemaCarryPlan.install_prepared(
            acquired.paths[0].parents[1], candidate, (store,), acquired, destination, schema,
            classification='goal/declaration-preserve')
        with closing(sqlite3.connect(original.path.as_uri()+'?mode=ro', uri=True)) as db:
            cls.require_schema(db)
        return result


class GoalDocumentNativeStore(CarriedNativeStore):
    """A retained document containing Goal values, not execution permissions."""

    @classmethod
    @abstractmethod
    def goals(cls, path): ...

    @classmethod
    @abstractmethod
    def convert(cls, path, conversion): ...


class GoalJsonNativeStore(GoalDocumentNativeStore):
    @classmethod
    @abstractmethod
    def slots(cls, document): ...

    @classmethod
    def goals(cls, path):
        from agent_comms.bus_publication import unique_wire_object
        document = json.loads(path.read_bytes(), object_pairs_hook=unique_wire_object)
        return tuple(cls.slots(document))

    @classmethod
    def convert(cls, path, conversion):
        from agent_comms.bus_publication import unique_wire_object
        document = json.loads(path.read_bytes(), object_pairs_hook=unique_wire_object)
        authors_converted = sum(conversion.goal(goal) for goal in cls.slots(document))
        path.write_text(json.dumps(document, indent=2, allow_nan=False) + '\n')
        return authors_converted


class GoalRegistryNativeStore(GoalJsonNativeStore):
    name = 'registry.json'

    @classmethod
    def slots(cls, document):
        return (thread['goal'] for thread in document['threads'].values())

    def publisher(self, root, custody):
        from agent_comms.registry_store import RegistryStore
        store = RegistryStore(root / self.name)
        custody.enter_context(store.locked())
        def publish(staging, original):
            # The existing registry writer owns its two-file private guard.
            original.require_original()
            store._write_unlocked(staging.path.read_text())
        return publish


class GoalReleaseNativeStore(GoalJsonNativeStore):
    name = 'owner_release_receipts.json'

    @classmethod
    def slots(cls, document):
        return (receipt['thread']['goal'] for receipt in document.values())


class GoalHistoryNativeStore(GoalDocumentNativeStore):
    name = 'goal_history.sqlite3'

    @classmethod
    def goals(cls, path):
        from agent_comms.goal_history import GoalHistoryEntry
        with closing(sqlite3.connect(path.as_uri() + '?mode=ro', uri=True)) as db:
            return tuple(json.loads(value) if value is not None else None
                         for row in db.execute(f'SELECT "before","after" FROM {quoted(GoalHistoryEntry.declared_name)}')
                         for value in row)

    @classmethod
    def convert(cls, path, conversion):
        from agent_comms.goal_history import GoalHistoryEntry
        from agent_comms.goals import Goal
        with closing(sqlite3.connect(path)) as db:
            original = inventory(db)
            table = GoalHistoryEntry.declared_name
            authors_converted = 0
            for sequence, before, after in db.execute(f'SELECT sequence,"before","after" FROM {quoted(table)}').fetchall():
                converted = []
                for value in (before, after):
                    goal = json.loads(value) if value is not None else None
                    authors_converted += conversion.goal(goal)
                    converted.append(FieldCodec.decode(Goal | None, goal))
                GoalHistoryEntry.update(db, where='sequence=?', parameters=(sequence,),
                                        before=converted[0], after=converted[1])
            # Every sequence, state (including uncertain), timestamp and owner
            # must remain exact. Only the two declared Goal cells may differ.
            old = original[table]
            new = inventory(db)[table]
            if [row[:3] + row[5:] for row in old] != [row[:3] + row[5:] for row in new]:
                raise ValueError('Goal history carry changed a non-Goal fact')
            GoalHistoryEntry.select(db)
            db.commit()
            return authors_converted


@dataclass(frozen=True)
class GoalFormatDeclaration:
    goal_fields: tuple[str, ...]
    mention_fields: tuple[str, ...]
    history: dict[str, str]
    todo: dict[str, str]

    @classmethod
    def observe(cls):
        from agent_comms.goals import Goal, GoalMentionSource
        from agent_comms.goal_history import GoalHistoryEntry
        from agent_comms.todos import Todo
        return cls(tuple(item.name for item, _ in FieldCodec.record_fields(Goal)),
                   tuple(item.name for item, _ in FieldCodec.record_fields(GoalMentionSource)),
                   GoalHistoryEntry.schema_objects(), Todo.schema_objects())

    def goal(self, raw):
        """One conversion, shared by current, historical and retired Goals."""
        from agent_comms.goals import Goal, UncommandedGoalAuthor
        if raw is None:
            return 0
        before = json.loads(json.dumps(raw, allow_nan=False))
        target = type(self).observe()
        if set(self.goal_fields) - set(target.goal_fields):
            raise ValueError('Goal carry cannot discard an original field')
        difference = set(target.mention_fields) - set(self.mention_fields)
        if set(self.mention_fields) - set(target.mention_fields) or difference not in (set(), {'author'}):
            raise ValueError('Unreviewed Goal mention declaration conversion')
        source = raw.get('mention_source')
        authors_converted = 0
        if source is not None and difference:
            if 'author' in source:
                raise ValueError('Original declaration did not declare the stored author')
            # Pre-P107 had no commanded Goals. Absence of a mention source is
            # NOT permission to manufacture a source, bindings or author proof.
            source['author'] = FieldCodec.encode(UncommandedGoalAuthor())
            authors_converted = 1
        FieldCodec.decode(Goal, raw)
        restored = json.loads(json.dumps(raw, allow_nan=False))
        if source is not None and difference:
            del restored['mention_source']['author']
        if restored != before:
            raise ValueError('Goal carry changed an original Goal fact')
        return authors_converted


def original_goal_formats(root):
    """Authentic installed source codec validates the exact copied originals."""
    from agent_comms.goals import Goal
    from agent_comms.goal_history import GoalHistoryEntry
    from agent_comms.todos import Todo
    declared = GoalFormatDeclaration.observe()
    originals = {}
    for member in CarriedNativeStore.members_with(GoalDocumentNativeStore):
        path = root / member.name
        if not path.exists():
            continue
        if member is GoalHistoryNativeStore:
            with closing(sqlite3.connect(path.as_uri() + '?mode=ro', uri=True)) as db:
                if objects(db) != declared.history:
                    raise ValueError('Original Goal history declaration differs')
                GoalHistoryEntry.select(db)
        for goal in member.goals(path):
            FieldCodec.decode(Goal | None, goal)
        originals[member.name] = digest(path)
    todo = root / 'todos.sqlite3'
    if todo.exists():
        with closing(sqlite3.connect(todo.as_uri() + '?mode=ro', uri=True)) as db:
            if objects(db) != declared.todo:
                raise ValueError('Original Todo declaration differs')
            Todo.select(db)
        originals[todo.name] = digest(todo)
    import agent_comms
    return {'source_package': agent_comms.__file__, 'declaration': FieldCodec.encode(declared),
            'originals': originals}


class RuntimeGoalFormatFiles(RuntimeCompactionFiles):
    @property
    def paths(self):
        documents = tuple(self.root / member.name for member in
                          CarriedNativeStore.members_with(GoalDocumentNativeStore))
        return (*documents, self.root / '.registry-owner-guard', self.root / 'todos.sqlite3',
                *(Path(str(path) + suffix) for path in documents if path.suffix == '.sqlite3'
                  for suffix in ('-journal', '-wal', '-shm')))

    def acquire(self):
        if any(path.exists() or path.is_symlink() for path in self.paths
               if path.name.endswith(('-journal', '-wal', '-shm'))):
            raise ValueError('Original Goal companion requires stopped review')
        return super().acquire()


@dataclass(frozen=True)
class GoalFormatCarryPlan:
    """Prepared copies only. Publication requires the original stopped handoff."""
    root: Annotated[Path, PathText]
    candidate: Annotated[Path, PathText]
    source: GoalFormatDeclaration
    stores: tuple[CarriedNativeStore, ...]

    @classmethod
    def prepare(cls, root, candidate, source_python):
        root, candidate = root.absolute(), candidate.absolute()
        if candidate == root or candidate.is_relative_to(root):
            raise ValueError('Goal candidate must be separate persistent owned storage')
        candidate.mkdir(mode=0o700)
        # This command never writes or opens a writer against the source root.
        # Live copies are diagnostic evidence only; the stopped operation must
        # acquire fresh originals and prepare again under its existing wire OFD.
        members = CarriedNativeStore.members_with(GoalDocumentNativeStore)
        for member in members:
            path = root / member.name
            if path.exists():
                if any(Path(str(path) + suffix).exists() for suffix in ('-journal', '-wal', '-shm')):
                    raise ValueError('Original Goal companion requires stopped review')
                retain_file(path, candidate / member.name)
        todo = root / 'todos.sqlite3'
        if todo.exists():
            raise ValueError('Existing Todo store requires explicit assignment/acceptance carry; no reset or invented facts')
        environment = dict(os.environ)
        environment.pop('PYTHONPATH', None)
        captured = subprocess.run([str(source_python), str(Path(__file__).resolve()),
                                  '--goal-formats', str(candidate)], env=environment,
                                 capture_output=True, text=True, check=True, timeout=30)
        packet = json.loads(captured.stdout)
        if not Path(packet['source_package']).is_relative_to(source_python.absolute().parent.parent):
            raise ValueError('Goal decoder did not use the installed source package')
        source = FieldCodec.decode(GoalFormatDeclaration, packet['declaration'])
        stores = []
        for member in members:
            path = candidate / member.name
            if not path.exists():
                continue
            original = packet['originals'][member.name]
            if digest(path) != original or digest(root / member.name) != original:
                raise ValueError('Original Goal document changed during capture')
            authors_converted = member.convert(path, source)
            with path.open('rb') as opened:
                os.fsync(opened.fileno())
            stores.append(member(original, digest(path), {
                'original_facts_preserved': True, 'authors_converted': authors_converted}))
        fsync_directory(candidate)
        plan = cls(root, candidate, source, tuple(stores))
        write_original(candidate / 'goal-format-plan.json',
                       (json.dumps(FieldCodec.encode(plan), indent=2) + '\n').encode())
        fsync_directory(candidate)
        return plan

    def install(self, stopped, destination):
        """Accept the existing inherited wire; never stop, fence or signal here."""
        if Path(stopped.handoff.root) != self.root:
            raise ValueError('Stopped handoff names another Goal root')
        if any(owner.launch.process.alive() for owner in stopped.handoff.owners):
            raise ValueError('Goal conversion requires the original owners stopped')
        with RuntimeGoalFormatFiles(self.root).acquire() as acquired:
            originals = {item.path: item for item in acquired.originals}
            for store in self.stores:
                if originals[self.root / store.name].sha256 != store.original_sha256:
                    raise ValueError('Stopped Goal document changed after preparation')
                if digest(self.candidate / store.name) != store.candidate_sha256:
                    raise ValueError('Prepared Goal candidate changed')
            result = NativeSchemaCarryPlan.install_prepared(
                self.root, self.candidate, self.stores, acquired, destination, self,
                classification='goal-format/original-source-carry')
            changed = {self.root / store.name for store in self.stores}
            changed.add(self.root / '.registry-owner-guard')
            acquired.unchanged_by(changed).require_original()
        from agent_comms.registry_store import RegistryStore
        RegistryStore(self.root / 'registry.json').private_guard_unlocked()
        return result

    def verify_installed(self, destination):
        """Target codec verifies bytes installed by the original stopped owner."""
        # Check installed semantics against the actual retained preimages, not
        # a regenerated history or a claim inferred from successful replacement.
        members = {member.name: member for member in
                   CarriedNativeStore.members_with(GoalDocumentNativeStore)}
        for store in self.stores:
            member = members[store.name]
            before = member.goals(destination / store.name)
            after = member.goals(self.root / store.name)
            if len(before) != len(after):
                raise ValueError('Installed Goal document lost a retained value')
            for original, installed in zip(before, after, strict=True):
                self.source.goal(original)
                if original != installed:
                    raise ValueError('Installed Goal differs from its retained original')


class CoordinationNativeStore(NativeReleaseStore):
    name = 'coordination.sqlite3'

    @classmethod
    def carry(cls, db, original, target, requests):
        return carry_coordination(db, original, target)


class PromptBindingNativeStore(NativeReleaseStore):
    name = 'native_prompt_bindings.sqlite3'

    @classmethod
    def carry(cls, db, original, target, requests):
        return carry_binding(db, original, target)

    def publisher(self, root, custody):
        from agent_comms.private_sidecar import _locked_directory, _read_snapshot, _publish

        directory = custody.enter_context(_locked_directory(root / self.name))
        snapshot = _read_snapshot(directory, self.name)
        if snapshot is None:
            raise ValueError('Original prompt binding disappeared')
        def publish(staging, original):
            _publish(directory, self.name, snapshot[1], staging.path.read_bytes())
        return publish


class CompactionNativeStore(NativeReleaseStore):
    name = 'compaction-commits.sqlite3'

    @classmethod
    def carry(cls, db, original, target, requests):
        return carry_compaction(db, original, target, requests)


@dataclass(frozen=True)
class NativeSchemaCarryPlan:
    root: Annotated[Path, PathText]
    candidate: Annotated[Path, PathText]
    original: NativeSchemaDeclaration
    target: NativeSchemaDeclaration
    stores: tuple[CarriedNativeStore, ...]

    @classmethod
    def prepare(cls, root, candidate, original, source_python):
        target = NativeSchemaDeclaration.observe()
        original.require_carry_target(target)
        if not source_python.is_absolute() or not source_python.is_file():
            raise ValueError('Authentic original installed interpreter is required')
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
            compaction = candidate / CompactionNativeStore.name
            requests = target.capture_requests(compaction, source_python, original) if compaction.exists() else []
            owners = {store.name:store for store in CarriedNativeStore.members_with(NativeReleaseStore)}
            for item in acquired.originals:
                path = candidate / item.path.name
                with closing(sqlite3.connect(path, isolation_level=None)) as db:
                    db.execute('PRAGMA foreign_keys=OFF')
                    db.execute('PRAGMA synchronous=FULL')
                    db.execute('BEGIN IMMEDIATE')
                    try:
                        evidence = owners[item.path.name].carry(db, original, target, requests)
                        db.execute('COMMIT')
                    except BaseException:
                        db.execute('ROLLBACK')
                        raise
                with path.open('rb') as saved:
                    os.fsync(saved.fileno())
                stores.append(owners[item.path.name](item.sha256, digest(path), evidence))
            acquired.require_original()
        fsync_directory(candidate)
        plan = cls(root, candidate, original, target, tuple(stores))
        plan.require_candidate()
        return plan

    def require_candidate(self):
        self.original.require_carry_target(self.target)
        if NativeSchemaDeclaration.observe() != self.target:
            raise ValueError('Matched target declarations changed after carry preparation')
        declared_paths = RuntimeNativeFiles(self.candidate).paths
        if tuple(item.name for item in self.stores) != tuple(
                path.name for path in declared_paths if path.suffix == '.sqlite3' and path.exists()):
            raise ValueError('Carry must cover exactly the original native and compaction stores')
        PrivateDirectoryRole.require(self.candidate.lstat())
        with RuntimeNativeFiles(self.candidate).acquire() as acquired:
            observed = {item.path.name:item.sha256 for item in acquired.originals}
            if observed != {item.name:item.candidate_sha256 for item in self.stores}:
                raise ValueError('Reviewed native candidate changed')

    def install(self, destination):
        self.require_candidate()
        with ExitStack() as custody:
            acquired = custody.enter_context(RuntimeNativeFiles(self.root).acquire())
            by_name = {item.path.name:item for item in acquired.originals}
            if set(by_name) != {item.name for item in self.stores}:
                raise ValueError('Original native store membership changed')
            if any(by_name[item.name].sha256 != item.original_sha256 for item in self.stores):
                raise ValueError('Original native preimage changed after review')
            return self.install_prepared(self.root, self.candidate, self.stores,
                                         acquired, destination, self)

    @staticmethod
    def install_prepared(root, candidate, stores, acquired, destination, review,
                         classification='runtime/declaration-original-source-carry'):
        """One retained-preimage/publication lifetime for every carried store."""
        with ExitStack() as custody:
            publishers = {item.name:item.publisher(root, custody) for item in stores}
            return NativeSchemaCarryPlan.publish_prepared(
                root, candidate, stores, acquired, destination, review, publishers, classification)

    @staticmethod
    def publish_prepared(root, candidate, stores, acquired, destination, review,
                         publishers, classification):
        destination.mkdir(mode=0o700)
        PrivateDirectoryRole.require(destination.lstat())
        write_original(destination / 'reviewed-carry.json',
                       (json.dumps(FieldCodec.encode(review), indent=2)+'\n').encode())
        for original in acquired.originals:
            retained = destination / original.path.relative_to(root)
            parent = retained.parent
            if parent != destination:
                parent.mkdir(mode=0o700, exist_ok=True)
            retain_file(original.path, retained)
            fsync_directory(parent)
        write_original(destination / 'reviewed-stores.json',
                       (json.dumps(FieldCodec.encode(stores), indent=2)+'\n').encode())
        fsync_directory(destination)
        fsync_directory(destination.parent)
        acquired.require_original()
        # All preimages and held candidates exist before the first replace.
        # Any exception leaves the existing stopped batch and this attempt
        # directory intact. No implicit rollback, launch or retry.
        originals = {original.path: original for original in acquired.originals}
        with ExitStack() as staging_custody:
            staged = [(item, staging_custody.enter_context(
                originals[root / item.name].prepare_replacement(
                    candidate / item.name, item.candidate_sha256))) for item in stores]
            write_original(destination / 'publication-staging.json',
                           (json.dumps([stage.evidence() for _, stage in staged], indent=2)+'\n').encode())
            fsync_directory(destination)
            acquired.require_original()
            for item, stage in staged:
                publishers[item.name](stage, originals[root / item.name])
            if any(digest(root / item.name) != item.candidate_sha256 for item in stores):
                raise ValueError('Installed native carry differs; remain stopped')
            result = {'classification':classification, 'stores':FieldCodec.encode(stores),
                      'original_preimages':str(destination), 'retired':[], 'reconstructed_proofs':0, 'input_replays':0}
            write_original(destination / 'installed.json', (json.dumps(result,indent=2)+'\n').encode())
            fsync_directory(destination)
        return result



if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--declaration', action='store_true')
    parser.add_argument('--compaction-requests', type=Path)
    parser.add_argument('--goal-declaration', action='store_true')
    parser.add_argument('--goal-formats', type=Path)
    parser.add_argument('--goal-format-candidate', type=Path)
    parser.add_argument('--source-root', type=Path)
    parser.add_argument('--source-python', type=Path)
    args = parser.parse_args()
    if args.goal_formats:
        print(json.dumps(original_goal_formats(args.goal_formats)))
    elif args.goal_format_candidate:
        if args.source_root is None or args.source_python is None:
            parser.error('Goal candidate requires --source-root and --source-python')
        print(json.dumps(FieldCodec.encode(GoalFormatCarryPlan.prepare(
            args.source_root, args.goal_format_candidate, args.source_python))))
    elif args.goal_declaration:
        print(json.dumps(GoalNativeStore.schema()))
    elif args.declaration:
        print(json.dumps(FieldCodec.encode(NativeSchemaDeclaration.observe())))
    elif args.compaction_requests:
        print(json.dumps(original_compaction_requests(args.compaction_requests), allow_nan=False))
    else:
        parser.error('Only declaration or original-request observation is supported; carry is owned by stopped RuntimeInstallation')
