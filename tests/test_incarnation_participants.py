from dataclasses import replace

import pytest

from agent_comms.comms import wire
from agent_comms.coordination.database import CoordinationStore
from agent_comms.coordination.errors import CoordinationError, IdentityConflict
from agent_comms.coordination.participant_store import ParticipantStore
from agent_comms.coordination.session import CoordinationSession
from agent_comms.coordination.tables.participants import Participants
from agent_comms.projects.checkouts import Checkout
from agent_comms.registry.thread_identity import (
    AgentThreadRole,
    UserThreadRole,
)
from agent_comms.registry.thread_text import GivenThreadText
from agent_comms.registry.threads import NativeThread
from bus_record_fixture import UNRELATED_LOOKUP


def test_incarnation_has_one_participant_before_registration_returns(tmp_path):
    comms = wire(tmp_path / "wire")
    first = comms.registry.declare(NativeThread("person", frozenset(), Checkout(tmp_path), role=UserThreadRole))
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
        raise CoordinationError("participant creation failed")

    with monkeypatch.context() as changed:
        changed.setattr(ParticipantStore, "register", failed)
        with pytest.raises(CoordinationError, match="participant creation failed"):
            comms.registry.declare(NativeThread("missing", frozenset(), Checkout(tmp_path)))
    assert "missing" not in comms.registry.snapshot().threads


def test_existing_public_participant_survives_native_root_preparation(tmp_path):
    comms = wire(tmp_path / "wire")
    thread = comms.registry.declare(NativeThread("public-first", frozenset(), Checkout(tmp_path)))
    with CoordinationSession(str(CoordinationStore.path_in(comms.root))) as session:
        before = ParticipantStore(session).get(thread.incarnation.lookup)
    comms.registry.store.prepare_native_root()
    with CoordinationSession(str(CoordinationStore.path_in(comms.root))) as session:
        assert ParticipantStore(session).get(thread.incarnation.lookup) == before


def test_missing_participant_reader_names_lookup(tmp_path):
    with CoordinationSession(str(CoordinationStore.path_in(tmp_path))) as session:
        with pytest.raises(IdentityConflict, match=UNRELATED_LOOKUP):
            ParticipantStore(session).get(UNRELATED_LOOKUP)
