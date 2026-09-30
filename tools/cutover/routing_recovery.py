"""Private recovery resources for one stopped-batch operation, never readers."""
from contextlib import closing
import json
import os
from pathlib import Path
import sqlite3

from agent_comms.field_codec import FieldCodec
from routing_carry import file_witness


def write_original(path: Path, value: bytes) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, 'wb') as output:
        output.write(value)
        output.flush()
        os.fsync(output.fileno())


def retain_originals(service, receipt: Path, plan) -> None:
    """Persist exact preimages before either schema/index or routing changes."""
    destination = receipt.with_name(receipt.name + '.originals')
    destination.mkdir(mode=0o700)
    sources = (service.root / 'registry.json', service.bus.log.metadata_path,
               service.registry.store.private_guard_unlocked().path)
    retained = {}
    for source in sources:
        target = destination / source.name
        write_original(target, source.read_bytes())
        retained[source.name] = file_witness(target)
    database = service.root / 'transcript_routes.sqlite3'
    if database.exists():
        target = destination / database.name
        write_original(target, b'')
        with (closing(sqlite3.connect(database.as_uri() + '?mode=ro', uri=True)) as original,
              closing(sqlite3.connect(target)) as backup):
            backup.execute('PRAGMA synchronous=FULL')
            original.backup(backup)
        descriptor = os.open(target, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        retained[database.name] = file_witness(target)
    write_original(destination / 'prepared-plan.json',
                   json.dumps(FieldCodec.encode(plan), indent=2).encode())
    write_original(destination / 'manifest.json', json.dumps({
        'purpose': 'Stopped carry recovery preimages; not runtime state or input replay authority',
        'original_database': plan.database, 'retained_files': retained,
    }, indent=2).encode())
    for directory in (destination, destination.parent):
        descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
