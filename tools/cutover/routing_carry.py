"""Transient one-shot plan, consumed under original stopped-batch custody.

The old interpreter decodes its own annotations. The target decodes only the
prepared reference format. No production reader learns the replaced format.
"""
from contextlib import closing
from dataclasses import dataclass, fields
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import stat
from typing import Any

from agent_comms.field_codec import FieldCodec
from agent_comms.transcript_routes import RouteAnnotationTable, _assert_schema, _schema
from agent_comms.typed_table import Column, TypedTable


def digest_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def digest_json(value) -> str:
    return digest_bytes(json.dumps(value, sort_keys=True, separators=(',', ':')).encode())


def file_witness(path: Path) -> dict[str, Any]:
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return {'missing': True}
    with os.fdopen(descriptor, 'rb') as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_nlink != 1:
            raise ValueError('Carry source must be original owned regular storage.')
        return {'sha256': hashlib.file_digest(stream, 'sha256').hexdigest(),
                'mode': stat.S_IMODE(info.st_mode), 'device': info.st_dev,
                'inode': info.st_ino, 'size': info.st_size}


def tables():
    return tuple(TypedTable.members_with(RouteAnnotationTable))


def primary_keys(table):
    return tuple(item.name for item in fields(table)
                 if item.metadata.get('sql', Column()).primary_key)


def table_rows(connection, table):
    keys = primary_keys(table)
    if not keys or 'routing' not in table.columns():
        raise ValueError('Annotation declaration lacks routing/key ownership.')
    names = table._column_list(table.columns())
    order = table._column_list(keys)
    return [dict(zip(table.columns(), row, strict=True)) for row in
            connection.execute(f'SELECT {names} FROM "{table.declared_name}" ORDER BY {order}')]


def registry_without_routing(document):
    copied = json.loads(json.dumps(document))
    for thread in copied['threads'].values():
        turn = thread.get('active_turn')
        if turn is not None and 'routing' in turn:
            turn['routing'] = None
    return copied


@dataclass(frozen=True)
class RoutingCell:
    table: str
    keys: dict[str, Any]
    original_digest: str
    routing: dict[str, Any]


@dataclass(frozen=True)
class RegistryRouting:
    thread: str
    original_digest: str
    routing: dict[str, Any]


@dataclass(frozen=True)
class RoutingCarryPlan:
    root_id: str
    bus: dict[str, Any]
    schema: dict[str, str]
    annotation_nonrouting_digest: str
    registry_digest: str
    registry_nonrouting_digest: str
    database: dict[str, Any]
    cells: tuple[RoutingCell, ...]
    turns: tuple[RegistryRouting, ...]
    originals: tuple[dict[str, Any], ...]
    protected: dict[str, dict[str, Any]]

    def require_sources(self, root: Path) -> None:
        if file_witness(root / 'bus.jsonl') != self.bus:
            raise ValueError('Original certified wire changed during carry.')
        for path, original in self.protected.items():
            if file_witness(Path(path)) != original:
                raise ValueError('Protected original changed during carry.')

    def require_target(self, root: Path, connection):
        """Validate every target row/identity before the first update."""
        from agent_comms.routing import TurnRouting

        self.require_sources(root)
        registry_bytes = (root / 'registry.json').read_bytes()
        document = json.loads(registry_bytes)
        if digest_bytes(registry_bytes) != self.registry_digest:
            raise ValueError('Original registry changed during carry.')
        if digest_json(registry_without_routing(document)) != self.registry_nonrouting_digest:
            raise ValueError('Original nonrouting registry differs.')
        available = {table.declared_name: table for table in tables()}
        originals = {}
        rows_without_routing = {}
        if connection is not None:
            _assert_schema(connection)
            if _schema(connection) != self.schema:
                raise ValueError('Annotation schema differs from original declaration.')
            for name, table in available.items():
                rows = table_rows(connection, table)
                originals[name] = {tuple(row[key] for key in primary_keys(table)): row for row in rows}
                rows_without_routing[name] = [{k: v for k, v in row.items() if k != 'routing'} for row in rows]
        if digest_json(rows_without_routing) != self.annotation_nonrouting_digest:
            raise ValueError('Original annotation resources differ.')
        expected_cells = {(name, key) for name, rows in originals.items()
                          for key, row in rows.items() if row['routing'] is not None}
        actual_cells = {(cell.table, tuple(cell.keys[key] for key in primary_keys(available[cell.table])))
                        for cell in self.cells}
        if actual_cells != expected_cells or len(actual_cells) != len(self.cells):
            raise ValueError('Prepared carry does not cover every original routing cell exactly once.')
        for cell in self.cells:
            table = available[cell.table]
            if set(cell.keys) != set(primary_keys(table)):
                raise ValueError('Original annotation primary key differs.')
            row = originals[cell.table][tuple(cell.keys[key] for key in primary_keys(table))]
            if digest_bytes(row['routing'].encode()) != cell.original_digest:
                raise ValueError('Original routing cell changed.')
            TurnRouting.from_wire(cell.routing)
        expected_turns = {name for name, thread in document['threads'].items()
                          if thread['active_turn'] is not None
                          and thread['active_turn'].get('routing') is not None}
        if {turn.thread for turn in self.turns} != expected_turns or len(expected_turns) != len(self.turns):
            raise ValueError('Prepared carry does not cover every original registry routing exactly once.')
        for turn in self.turns:
            routing = document['threads'][turn.thread]['active_turn']['routing']
            if digest_json(routing) != turn.original_digest:
                raise ValueError('Original active-turn routing changed.')
            TurnRouting.from_wire(turn.routing)
        return document


def prepare_original(service, marker) -> RoutingCarryPlan:
    """Only the original interpreter may decode embedded Message requests."""
    from agent_comms.bus_publication import unique_wire_object
    from agent_comms.routing import TurnRouting
    from agent_comms.messages import Message

    root = service.root
    database = root / 'transcript_routes.sqlite3'
    registry_bytes = (root / 'registry.json').read_bytes()
    document = json.loads(registry_bytes, object_pairs_hook=unique_wire_object)
    # Keep only referenced originals, not a second full wire history.
    wanted = {}

    def convert(raw):
        data = json.loads(raw, object_pairs_hook=unique_wire_object)
        old = TurnRouting.from_wire(data)
        for encoded, decoded in zip(data['requests'], old.requests, strict=True):
            if Message.from_committed_wire(encoded) != decoded:
                raise ValueError('Embedded request is not its exact stored public envelope.')
        converted = dict(data)
        converted['requests'] = [FieldCodec.encode(message.reference) for message in old.requests]
        for message in old.requests:
            prior = wanted.setdefault(message.reference, message)
            if prior != message:
                raise ValueError('Embedded requests disagree on original envelope.')
        return converted

    cells, turns, schema, nonrouting = [], [], {}, {}
    native_files = {thread.get('session_file') for thread in document['threads'].values()}
    if database.exists():
        with closing(sqlite3.connect(database.as_uri() + '?mode=ro', uri=True)) as db:
            _assert_schema(db)
            schema = _schema(db)
            for table in tables():
                rows = table_rows(db, table)
                nonrouting[table.declared_name] = [{k: v for k, v in row.items() if k != 'routing'} for row in rows]
                for row in rows:
                    if row.get('session_file'):
                        native_files.add(row['session_file'])
                    if row['routing'] is not None:
                        cells.append(RoutingCell(table.declared_name,
                            {key: row[key] for key in primary_keys(table)},
                            digest_bytes(row['routing'].encode()), convert(row['routing'])))
    for name, thread in document['threads'].items():
        turn = thread.get('active_turn')
        if turn is not None and turn.get('routing') is not None:
            turns.append(RegistryRouting(name, digest_json(turn['routing']),
                                         convert(json.dumps(turn['routing']))))
    publications = set()
    from agent_comms.message_reference import MessageReference
    for routing in (cell.routing for cell in (*cells, *turns)):
        publications.update(FieldCodec.decode(MessageReference, item)
                            for item in routing.get('publications', ()))
    required = {*wanted, *publications}
    originals = []
    for record in service.bus.log.verified_records_unlocked(marker):
        reference = record.message.reference
        if reference not in required:
            continue
        deliveries = record.deliveries()
        if len(deliveries) != 1:
            raise ValueError('Routing reference lacks its original frozen delivery.')
        delivery = deliveries[0]
        copied = wanted.get(reference)
        if copied is not None and copied.publication_snapshot != delivery.message.publication_snapshot:
            raise ValueError('Embedded request differs from original certified publication.')
        originals.append({'reference': FieldCodec.encode(reference),
            'sender_lookup': delivery.audience.sender_lookup,
            'recipient_lookups': [item.recipient_lookup for item in delivery.audience.recipients]})
    if {FieldCodec.decode(MessageReference, item['reference']) for item in originals} != required:
        raise ValueError('Routing references are absent from the original certified wire.')
    paths = {root / 'input_dispositions.json', root / 'native_prompt_bindings.sqlite3',
             root / 'read_ledger.json', root / 'activity.jsonl'}
    for name in native_files - {None, ''}:
        path = Path(name).absolute()
        paths.update((path, Path(str(path) + '.input-proof')))
    for source in tuple(paths):
        paths.update((Path(str(source) + '-wal'), Path(str(source) + '-shm')))
    return RoutingCarryPlan(marker.root_id, file_witness(root / 'bus.jsonl'), schema,
        digest_json(nonrouting), digest_bytes(registry_bytes),
        digest_json(registry_without_routing(document)),
        file_witness(database),
        tuple(cells), tuple(turns), tuple(originals),
        {str(path): file_witness(path) for path in sorted(paths)})
