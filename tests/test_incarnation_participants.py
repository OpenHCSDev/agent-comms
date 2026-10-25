from agent_comms.repo import Checkout
from dataclasses import replace

import pytest

from agent_comms.comms import wire
from agent_comms.coordination_database import CoordinationStore
from agent_comms.coordination_errors import IntegrityViolationError, ParticipantNotRegistered
from agent_comms.coordination_session import CoordinationSession
from agent_comms.coordination_tables.participants import Participants
from agent_comms.participant_store import ParticipantStore
from agent_comms.thread_identity import AgentThreadRole, UserThreadRole
from agent_comms.thread_text import GivenThreadText
from agent_comms.threads import Thread
from bus_record_fixture import UNRELATED_LOOKUP


def test_incarnation_has_one_participant_before_registration_returns(tmp_path):
    comms = wire(tmp_path / "wire")
    first = comms.registry.declare(Thread("person", frozenset(), Checkout.from_wire(str(tmp_path)), role=UserThreadRole))
    lookup = first.incarnation.lookup
    with CoordinationSession(str(CoordinationStore.path_in(comms.root))) as session:
        original = ParticipantStore(session).get(lookup)
        assert original.committed
        assert len(Participants.select(session._connection, participant_lookup=lookup)) == 1
    updated = comms.registry.declare(replace(first, role=AgentThreadRole, title=GivenThreadText("changed")))
    assert updated.incarnation == first.incarnation
    with CoordinationSession(str(CoordinationStore.path_in(comms.root))) as session:
        assert ParticipantStore(session).get(lookup) == original
        assert len(Participants.select(session._connection, participant_lookup=lookup)) == 1
    comms.registry.rename("person", "renamed")
    assert comms.registry.require("renamed").incarnation.lookup == lookup


def test_participant_failure_cannot_publish_registry_incarnation(tmp_path, monkeypatch):
    comms = wire(tmp_path / "wire")

    def failed(self, lookup, *, committed=False):
        raise IntegrityViolationError("participant creation failed")

    with monkeypatch.context() as changed:
        changed.setattr(ParticipantStore, "register", failed)
        with pytest.raises(IntegrityViolationError, match="participant creation failed"):
            comms.registry.declare(Thread("missing", frozenset(), Checkout.from_wire(str(tmp_path))))
    assert "missing" not in comms.registry.snapshot().threads


def test_existing_public_participant_survives_native_root_preparation(tmp_path):
    comms = wire(tmp_path / "wire")
    thread = comms.registry.declare(Thread("public-first", frozenset(), Checkout.from_wire(str(tmp_path))))
    with CoordinationSession(str(CoordinationStore.path_in(comms.root))) as session:
        before = ParticipantStore(session).get(thread.incarnation.lookup)
    comms.registry.prepare_native_root()
    with CoordinationSession(str(CoordinationStore.path_in(comms.root))) as session:
        assert ParticipantStore(session).get(thread.incarnation.lookup) == before


def test_missing_participant_reader_names_lookup(tmp_path):
    with CoordinationSession(str(CoordinationStore.path_in(tmp_path))) as session:
        with pytest.raises(ParticipantNotRegistered, match=UNRELATED_LOOKUP):
            ParticipantStore(session).get(UNRELATED_LOOKUP)
