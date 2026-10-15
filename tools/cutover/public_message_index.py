"""One-use public-message index installation from the installed reader.

Capture uses the original runtime's certified public stream. Preparation adds
only the new declaration to a copy of its checkpoint. Publication must run at
the existing stopped-owner boundary, with the original bus lock retained.
Remove this tool after the live cutover.
"""

from __future__ import annotations

import argparse
from contextlib import closing
from dataclasses import dataclass
import json
import os
from pathlib import Path
import shutil
import sqlite3
import time

from agent_comms.checkpoint_seals import FinalSeal, PrefixSeal
from agent_comms.field_codec import FieldCodec
from agent_comms.message_reference import MessageReference
from agent_comms.private_bus_checkpoint import PrefixCertificate
from agent_comms.private_path import FileRevision
from agent_comms.typed_table import SQLiteSchemaObject, TypedTable
from agent_comms.wire_log import WireLog
from agent_comms.wire_metadata import WireMetadata


@dataclass(frozen=True)
class CapturedMessages:
    marker: WireMetadata
    source: PrefixSeal
    references: tuple[tuple[str, MessageReference], ...]

    @classmethod
    def read(cls, directory: Path):
        return FieldCodec.decode(cls, json.loads((directory / 'messages.json').read_text()))

    @classmethod
    def capture(cls, source, directory: Path):
        source.require_open_prefix()
        started = time.perf_counter()
        messages = tuple(source.public_message_references())
        elapsed = time.perf_counter() - started
        packet = cls(source.marker, source.witness.seal(), messages)
        with closing(sqlite3.connect(directory / 'original.sqlite3')) as destination:
            source.connection.backup(destination)
        source.require_open_prefix()
        (directory / 'messages.json').write_text(json.dumps(FieldCodec.encode(packet)))
        (directory / 'capture.json').write_text(json.dumps({
            'messages': len(messages), 'public_identity_scan_ms': elapsed * 1000,
            'through_seq': source.witness.through_seq,
        }, indent=2))
        return packet

    def prepare(self, directory: Path):
        from agent_comms.private_bus_checkpoint import CheckpointTable, PublicMessages, _saved

        target = directory / 'candidate.sqlite3'
        with target.open('xb') as output, (directory / 'original.sqlite3').open('rb') as source:
            shutil.copyfileobj(source, output)
        target.chmod(0o600)
        with closing(sqlite3.connect(target)) as db:
            db.row_factory = sqlite3.Row
            expected = {name: sql for table in TypedTable.members_with(CheckpointTable)
                        if table is not PublicMessages
                        for name, sql in table.schema_objects().items()}
            actual = SQLiteSchemaObject.read(db.execute(
                'SELECT name,sql FROM sqlite_master WHERE sql IS NOT NULL'))
            if SQLiteSchemaObject.differences(actual, expected):
                raise ValueError('Source checkpoint differs beyond the public-message index')
            if PrefixCertificate.read_witness(db).seal() != self.source:
                raise ValueError('Captured messages belong to another checkpoint')
            with db:
                PublicMessages.create(db)
                previous = 0
                for sender, reference in self.references:
                    PublicMessages(reference.seq, previous, sender, reference.message_id).insert(db)
                    previous = reference.seq
                if previous != self.source.through_seq:
                    raise ValueError('Public stream does not reach its certified sequence')
            witness = _saved(db)
            if witness.seal() != self.source:
                raise ValueError('Index preparation changed the source certificate')
            # Both queries use the production declarations on actual saved data.
            continuity_sql = f'SELECT 1 FROM "{PublicMessages.declared_name}" WHERE {PublicMessages.gap} LIMIT 1'
            identity_sql = f'SELECT 1 FROM "{PublicMessages.declared_name}" WHERE sender=? AND message_id=? LIMIT 1'
            identities = [(sender, reference.message_id) for sender, reference in self.references[-1:]]
            timings = []
            for _ in range(5):
                started = time.perf_counter()
                gap = db.execute(continuity_sql).fetchone()
                for identity in identities:
                    if db.execute(identity_sql, identity).fetchone() is None:
                        raise ValueError('Captured original identity is missing from the index')
                timings.append((time.perf_counter() - started) * 1000)
            result = {
                'messages': len(self.references), 'indexed_queries_ms': timings,
                'contains_sequence_gap': gap is not None,
                'continuity_plan': [tuple(row) for row in db.execute('EXPLAIN QUERY PLAN ' + continuity_sql)],
                'identity_plans': [[tuple(row) for row in db.execute('EXPLAIN QUERY PLAN ' + identity_sql,
                                                                   identity)] for identity in identities],
            }
            (directory / 'prepared.json').write_text(json.dumps(result, indent=2))
        return target

    def publish(self, root: Path, directory: Path, descriptor: int):
        import fcntl
        from agent_comms.private_bus_checkpoint import _directory_sync, _saved

        opened = os.fstat(descriptor)
        named = (root / '.bus.jsonl.lock').stat()
        if (opened.st_dev, opened.st_ino) != (named.st_dev, named.st_ino):
            raise ValueError('Installation requires the retained original bus lock')
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        bus = WireLog(root / 'bus.jsonl')
        marker = bus.read_metadata_unlocked()
        checkpoint = root / 'private_bus_checkpoint.sqlite3'
        if marker != self.marker or FileRevision.capture(bus.path) != self.source.revision:
            raise ValueError('Live source changed after capture; no index was installed')
        with closing(sqlite3.connect(checkpoint)) as db:
            db.row_factory = sqlite3.Row
            witness = PrefixCertificate.read_witness(db)
            marker.seal.check_final(witness, checkpoint)
            if witness.seal() != self.source:
                raise ValueError('Live certificate changed after capture')
        target = directory / 'candidate.sqlite3'
        with closing(sqlite3.connect(target)) as db:
            db.row_factory = sqlite3.Row
            if _saved(db).seal() != self.source:
                raise ValueError('Prepared index changed before publication')
        # Keep both original files. A crash after replace fails closed against
        # the old seal until the same captured source is explicitly recovered.
        (directory / 'original-marker.json').write_bytes(bus.metadata_path.read_bytes())
        for path in (directory / 'original.sqlite3', directory / 'original-marker.json', target):
            with path.open('rb') as stream:
                os.fsync(stream.fileno())
            _directory_sync(path)
        os.replace(target, checkpoint)
        _directory_sync(checkpoint)
        marker.seal_with(FinalSeal.capture(witness, checkpoint))
        bus.write_metadata_unlocked(marker)


def capture(args):
    args.directory.mkdir(mode=0o700)
    with WireLog(args.root / 'bus.jsonl').certified_read() as source:
        CapturedMessages.capture(source, args.directory)


def prepare(args):
    CapturedMessages.read(args.directory).prepare(args.directory)


def publish(args):
    packet = CapturedMessages.read(args.directory)
    packet.prepare(args.directory)
    packet.publish(args.root, args.directory, args.descriptor)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(required=True)
    original = commands.add_parser('capture')
    original.add_argument('root', type=Path)
    original.add_argument('directory', type=Path)
    original.set_defaults(action=capture)
    target = commands.add_parser('prepare')
    target.add_argument('directory', type=Path)
    target.set_defaults(action=prepare)
    installation = commands.add_parser('publish')
    installation.add_argument('root', type=Path)
    installation.add_argument('directory', type=Path)
    installation.add_argument('descriptor', type=int)
    installation.set_defaults(action=publish)
    args = parser.parse_args()
    args.action(args)


if __name__ == '__main__':
    main()
