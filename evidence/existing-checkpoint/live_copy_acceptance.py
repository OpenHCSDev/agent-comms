"""Installed migration acceptance on an owned copy; never sends/replays inputs."""
from __future__ import annotations

import json
import os
import shutil
import sqlite3
import tempfile
from contextlib import closing
from pathlib import Path

from agent_comms import wire
from agent_comms.declarations import MessageBus, _store_lock
from agent_comms.private_bus_checkpoint import (
    certified_delivery_page_unlocked,
    install_private_bus_checkpoint,
    verify_private_bus_checkpoint_unlocked,
)


def main():
    source = wire()
    base = Path(__file__).resolve().parents[2] / '.artifacts'
    base.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='live-copy-', dir=base) as directory:
        root = Path(directory)
        with _store_lock(source._wire_lock_path), _store_lock(source.bus._path):
            for name in ('bus.jsonl', 'bus_meta.json', 'registry.json'):
                shutil.copy2(source.root / name, root / name)
            with closing(sqlite3.connect(f'{(source.root / "coordination.sqlite3").as_uri()}?mode=ro', uri=True)) as original, closing(sqlite3.connect(root / 'coordination.sqlite3')) as copy:
                original.backup(copy)
            os.chmod(root / 'coordination.sqlite3', 0o600)
        bus = MessageBus(root / "bus.jsonl", source.registry)
        bus_bytes = bus._path.read_bytes()
        inode = bus._path.stat().st_ino
        sql_bytes = (root / 'coordination.sqlite3').read_bytes()
        registry_bytes = (root / 'registry.json').read_bytes()
        marker = json.loads((root / 'bus_meta.json').read_text())
        with _store_lock(bus._path):
            initials = [initial for _, _, initial in bus._verified_private_rows_unlocked(marker) if initial is not None]
        witness = install_private_bus_checkpoint(bus)
        assert bus._path.read_bytes() == bus_bytes
        assert bus._path.stat().st_ino == inode
        assert (root / 'coordination.sqlite3').read_bytes() == sql_bytes
        assert (root / 'registry.json').read_bytes() == registry_bytes
        assert witness.root_id == marker['wire_root_id'] and witness.through_seq == marker['last_seq']
        recipients = {r.recipient_lookup for initial in initials for r in initial.audience.recipients}
        with _store_lock(bus._path):
            current = bus._private_marker_unlocked()
            assert verify_private_bus_checkpoint_unlocked(bus, current) == witness
            for lookup in recipients:
                _, page, more = certified_delivery_page_unlocked(bus, current, lookup)
                expected = [i.message.seq for i in initials if any(r.recipient_lookup == lookup for r in i.audience.recipients)]
                assert [i.message.seq for i in page] == expected and not more
        receipt = dict(installed=True, bus_bytes_unchanged=True, bus_inode_unchanged=True,
                       sql_unchanged=True, registry_unchanged=True, source_rows=witness.through_seq,
                       initial_rows=len(initials), recipient_pages=len(recipients),
                       no_provider_calls=True, no_live_mutation=True, success=True)
        Path(__file__).with_name('live-copy-result.json').write_text(json.dumps(receipt, indent=2)+'\n')
        print(json.dumps(receipt))


if __name__ == '__main__':
    main()
