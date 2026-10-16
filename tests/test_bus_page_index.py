"""Page queries prepare owned offsets but never rewrite published archives."""

from __future__ import annotations

import json
import sqlite3

import pytest

from agent_comms.bus_page_index import BusPageIndex, BusPageRow
from agent_comms.comms import Comms
from agent_comms.errors import RelationViolationError
from agent_comms.messages import InfoKind, Message
from agent_comms.private_bus_checkpoint import install_private_bus_checkpoint
from agent_comms.read_basis import DMDisplayScope
from agent_comms.store_files import _store_lock
from agent_comms.threads import Thread
from agent_comms.wire_metadata import ArchivedAccess, WritableAccess


def retained_source(root, count, *, access=ArchivedAccess()):
    comms = Comms(root)
    for name in ("a", "b", "c"):
        comms.registry.declare(Thread(name, frozenset(), str(root)))
    comms.messaging.initialize_private_initial_protocol()
    with _store_lock(comms.root / 'wire'), comms.bus.log.locked():
        with comms.bus.log.path.open("w") as output:
            for seq in range(1, count + 1):
                row = Message("a", "b" if seq % 3 == 0 else "c", f"m{seq}", InfoKind, seq=seq)
                output.write(json.dumps(row.to_wire()) + "\n")
        comms.bus.log.path.chmod(0o600)
        marker = comms.bus.log.read_metadata_unlocked(required=True)
        marker.last_seq = marker.admission_after_seq = count
        marker.checkpoint_version = marker.checkpoint_seal = None
        comms.bus.log.write_metadata_unlocked(marker)
        (root / "private_bus_checkpoint.sqlite3").unlink()
        install_private_bus_checkpoint(comms.bus.log, _bus_locked=True)
    with BusPageIndex(comms.bus.log.path) as index:
        assert index.sync()
    marker = comms.bus.log.read_metadata_unlocked(required=True)
    marker.access = access
    comms.bus.log.write_metadata_unlocked(marker)
    return comms.bus


def test_page_queries_prepare_owned_offsets_and_leave_archives_unchanged(tmp_path):
    empty = Comms(tmp_path / 'empty').bus
    assert empty.full_history_page().messages == ()
    scope = DMDisplayScope(frozenset({'a'}), frozenset({'b'}))
    assert not empty.conversation_older_unread(scope, before=1, seen=frozenset())
    assert not (empty.log.path.parent / 'bus_page_index.sqlite3').exists()

    bus = retained_source(tmp_path / 'owned', 9, access=WritableAccess())
    path = bus.log.path.with_name('bus_page_index.sqlite3')
    with sqlite3.connect(path) as db:
        db.execute(f"UPDATE {BusPageRow.declared_name} SET offset=0 WHERE seq=9")
    assert [message.seq for message in bus.incoming_page('b', after=6).messages] == [9]
    path.unlink()
    assert [message.seq for message in bus.incoming_page('b', after=6).messages] == [9]
    path.unlink()
    sqlite3.connect(path).close()
    assert [message.seq for message in bus.incoming_page('b', after=6).messages] == [9]

    archive = retained_source(tmp_path / 'archive', 9)
    assert [message.seq for message in archive.incoming_page('b', after=6).messages] == [9]
    path = archive.log.path.with_name('bus_page_index.sqlite3')
    with sqlite3.connect(path) as db:
        db.execute(f"UPDATE {BusPageRow.declared_name} SET offset=0 WHERE seq=9")
    original = (path.read_bytes(), archive.log.path.read_bytes())
    with pytest.raises(RelationViolationError, match='read-only'):
        archive.incoming_page('b', after=6)
    assert (path.read_bytes(), archive.log.path.read_bytes()) == original
