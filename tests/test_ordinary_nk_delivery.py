"""Ordinary Comms sends on explicitly marked private roots use the canonical
publication protocol."""

from __future__ import annotations
from agent_comms.messages import NoticeRole

import json
import os

import pytest

from agent_comms.child_process import ProcessIdentity
from agent_comms.comms import Comms
from agent_comms.errors import RelationViolationError
from agent_comms.messages import Message, InfoKind
from test_coordinated_runtime import tmp_path as private_root_fixture

tmp_path = private_root_fixture


def test_fresh_send_uses_canonical_publication_and_human_delivery_has_no_wake(tmp_path):
    comms = Comms(tmp_path / "wire")
    for name in ("sender", "beta"):
        comms.registry.declare(
            comms.registry.new_thread(name, frozenset(), str(tmp_path), process_identity=ProcessIdentity.capture(os.getpid()))
        )
    message = comms.messaging.send("sender", "beta", "ordinary send").message
    metadata = comms.bus.log.read_metadata_unlocked(required=True)
    assert metadata.private and metadata.claims
    initial = comms.bus.log.read_delivery_cohort(metadata.root_id, message.seq)
    assert initial.message == message
    assert initial.audience.canonical_members == frozenset({"beta"})
    viewer = comms.messaging.user_identity(str(tmp_path)).name
    reply = comms.messaging.send("sender", viewer, "human notification").message
    human = comms.bus.log.read_delivery_cohort(metadata.root_id, reply.seq)
    assert isinstance(human.message.role, NoticeRole)
    assert human.audience.recipients == ()
    assert human.decisions == ()
    assert comms.views.dm_display_page("sender", worktree=str(tmp_path)).messages[-1] == reply


def test_unmarked_existing_data_is_not_rewritten_or_appended_by_send(tmp_path):
    comms = Comms(tmp_path / "wire")
    for name in ("sender", "beta"):
        comms.registry.declare(
            comms.registry.new_thread(name, frozenset(), str(tmp_path), process_identity=ProcessIdentity.capture(os.getpid()))
        )
    stored = Message(
        sender="sender", target="beta", body="preserved history", type=InfoKind, seq=1
    )
    comms.bus.log.path.write_text(json.dumps(stored.to_wire()) + "\n")
    before = comms.bus.log.path.read_bytes()
    with pytest.raises(
        RelationViolationError
    ):
        comms.messaging.send("sender", "beta", "must not append").message
    assert comms.bus.log.path.read_bytes() == before
    assert not comms.bus.log.metadata_path.exists()
