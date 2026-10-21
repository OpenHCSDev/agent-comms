"""Attached retained history remains readable and refuses publication."""

import pytest

from agent_comms.bus import Bus
from agent_comms.comms import Comms
from agent_comms.channels import AnyChannel
from agent_comms.errors import RelationViolationError
from test_private_human_ingress import _root


def test_archived_snapshot_preserves_rows_and_refuses_new_publication(tmp_path):
    source, store, _root_id, _lookups = _root(tmp_path)
    store.close()
    sent = source.messaging.send("alice", "bob", "retained message").message.reference
    reader_root = tmp_path / "reader"
    Bus.install(Bus.path_in(reader_root))
    reader = Comms(reader_root)
    attached = reader.bus.history.attach(source.root)
    snapshot = Comms(attached.root)
    before = snapshot.bus.bus.revision()
    assert [message.reference for message in snapshot.views.history_page(AnyChannel.target()).messages] == [sent]
    with pytest.raises(RelationViolationError):
        snapshot.messaging.send("alice", "bob", "must never append")
    with pytest.raises(RelationViolationError):
        snapshot.registry.rename("alice", "changed-history")
    with pytest.raises(RelationViolationError):
        snapshot.registry.prepare_native_root()
    with snapshot.registry.store.locked():
        document = snapshot.registry.store.read_revision_unlocked().document.copy()
        document.purpose = source.registry.snapshot().purpose
        with pytest.raises(RelationViolationError):
            snapshot.registry.store.save_unlocked(document)
    assert snapshot.bus.bus.revision() == before
    attached.validate()
