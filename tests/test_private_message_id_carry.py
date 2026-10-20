"""A stopped bus rekeys its old checkpoint without discarding original rows."""

from contextlib import closing

import pytest

from conftest import publish_initial
from agent_comms.comms import Comms
from agent_comms.coordination_database import CoordinationStore
from agent_comms.errors import RelationViolationError
from agent_comms.checkpoint_seals import FinalSeal
from agent_comms.private_bus_checkpoint import CheckedPrefix, DeliverySources, PublicMessages
from agent_comms.store_files import _store_lock
from cutover.carry_private_message_ids import PrivateMessageIds


def test_carry_rebuilds_message_indexes_and_seal(tmp_path):
    comms = Comms(tmp_path / 'bus')
    for name in ('author', 'worker'):
        comms.registry.declare(comms.registry.new_thread(name, frozenset(), str(tmp_path)))
    comms.messaging.initialize_private_initial_protocol()
    original = publish_initial(comms, 'author', 'worker', 'original old peer order')
    bus = comms.bus.log
    checkpoint = comms.root / 'private_bus_checkpoint.sqlite3'
    with _store_lock(comms.root / 'wire'):
        with closing(CoordinationStore.writer(checkpoint)) as db, db:
            db.execute('DROP INDEX "delivery_sources_1_idx"')
            db.execute('DROP INDEX "public_messages_2_idx"')
        with closing(CoordinationStore.read_snapshot(checkpoint)) as db:
            witness = CheckedPrefix.read_witness(db)
        marker = bus.private_marker_unlocked()
        marker.seal_with(FinalSeal.capture(witness, checkpoint))
        bus.write_metadata_unlocked(marker)
    with pytest.raises(RelationViolationError, match='schema'):
        with bus.checked_read():
            pass
    assert PrivateMessageIds.carry(comms.root, apply=False).changed == 2
    with _store_lock(comms.root / 'wire'):
        result = PrivateMessageIds.carry(comms.root, apply=True)
    assert result.changed == 2
    assert set(result.rewritten) == {'private_bus_checkpoint.sqlite3', 'bus_meta.json'}
    assert PrivateMessageIds.carry(comms.root, apply=False).changed == 0
    with bus.checked_read() as source:
        assert source.message_by_id(original.message_id) == original
        for table in (DeliverySources, PublicMessages):
            plan = tuple(source.connection.execute(
                f'EXPLAIN QUERY PLAN SELECT * FROM {table.declared_name} '
                'WHERE message_id=? ORDER BY seq DESC', (original.message_id,)))
            assert any('USING' in row[-1] and 'INDEX' in row[-1] for row in plan)
