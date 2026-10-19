"""Refused sends share the delivery database, not a page-owned log."""

from contextlib import closing

import pytest

from agent_comms.coordination_database import CoordinationStore
from agent_comms.coordination_tables.refusals import SendRefusal
from agent_comms.errors import RelationViolationError, ToolRefusedError
from agent_comms.notification_assignment import NotificationAssignment
from tools.cutover.carry_send_refusals import SendRefusalOutcomes


def test_install_preserves_existing_coordination_rows(comms):
    SendRefusal.record(comms.root, "claude-ai", "worker", ToolRefusedError("fresh-root refusal"))
    path = NotificationAssignment.database_path(comms.root)
    # Represent the prior installed schema, keeping all other real coordination rows.
    with closing(CoordinationStore.writer(path)) as db, db:
        db.execute(f'DROP TABLE "{SendRefusal.declared_name}"')
        original = tuple(db.iterdump())
    with pytest.raises(RelationViolationError, match="installed"):
        SendRefusal.record(comms.root, "claude-ai", "worker", ToolRefusedError("refused"))
    assert SendRefusalOutcomes.carry(comms.root, apply=False).changed == 1
    with CoordinationStore.observing(path) as db:
        assert tuple(db.iterdump()) == original
    assert SendRefusalOutcomes.carry(comms.root, apply=True).changed == 1
    assert SendRefusalOutcomes.carry(comms.root, apply=True).changed == 0
    with CoordinationStore.observing(path) as db:
        kept = tuple(line for line in db.iterdump() if SendRefusal.declared_name not in line)
    assert kept == original
    SendRefusal.record(comms.root, "claude-ai", "worker", ToolRefusedError("original refusal"))
    first = SendRefusal.recent(comms.root, sender="claude-ai", target="worker")
    assert len(first) == 1 and first[0].reason == "original refusal"
    assert SendRefusal.recent(comms.root) == first  # Reads never append another event.
    assert SendRefusal.recent(comms.root, target="elsewhere") == ()
