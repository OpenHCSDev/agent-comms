"""The one-shot copy carry merges prefix roots without touching its source."""
import hashlib
from pathlib import Path

import pytest

from agent_comms.bus_publication import stable_thread_lookup
from agent_comms.bus_route_counts import RouteEntryRow, RouteIdentityRow, RouteTimeRow
from agent_comms.comms import Comms
from agent_comms.messages import InfoKind, Message
from agent_comms.routing import DeliveryMessage
from agent_comms.threads import Thread
from tools.cutover.carry_route_lookups_once import CopiedRouteStore, dry_run_copy


@pytest.mark.parametrize('retired', [False, True])
def test_copy_carry_merges_equal_time_chains_and_lists_unknown_birth(tmp_path, retired):
    root = tmp_path / 'source'
    root.mkdir()
    comms = Comms(root)
    comms.registry.declare(Thread('known', frozenset(), str(root), created_at=1.0))
    if retired:
        comms.registry.declare(Thread('gone', frozenset(), str(root), created_at=2.0))
        comms.registry.unregister('gone')
        comms.registry.remove('gone')
    store = CopiedRouteStore(root)
    lookup = stable_thread_lookup(1.0)
    with store.writing() as connection:
        for seq, sender, stamp in ((1, 'known', ''), (2, 'known', lookup), (3, 'gone', '')):
            message = Message(sender, 'viewer', 'fixture', InfoKind, seq=seq, timestamp=5.0)
            RouteEntryRow.record(connection, DeliveryMessage(message, stamp))
    before = hashlib.sha256(store.path.read_bytes()).hexdigest()
    result = dry_run_copy(root, tmp_path / 'copy')
    assert result.stamped == result.merged_keys == result.changed_entries == 1
    assert [row.first_seq for row in result.unresolvable] == [3]
    assert 'not install acceptance' in result.durability
    copied = CopiedRouteStore(Path(result.copied_index).parent)
    with copied.snapshot() as connection:
        rows = RouteEntryRow.select(connection, sender='known', order_by=('seq',))
        assert [row.sender_lookup for row in rows] == [lookup, lookup]
        assert [RouteTimeRow.require(connection, row.time_root).node.count for row in rows] == [1, 2]
        assert rows[0].previous_at_timestamp is None
        assert rows[1].previous_at_timestamp == rows[0].seq
        assert RouteIdentityRow.one(connection, target='viewer', sender='known',
                                    sender_lookup=lookup).first_seq == 1
        assert [row.first_seq for row in RouteIdentityRow.select(connection, sender_lookup='')] == [3]
    assert hashlib.sha256(store.path.read_bytes()).hexdigest() == before
