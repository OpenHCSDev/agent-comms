from dataclasses import replace

import pytest

from agent_comms.comms import wire
from agent_comms.coordination_database import CoordinationStore
from agent_comms.coordination_errors import IntegrityViolationError, ParticipantNotRegistered
from agent_comms.coordination_session import CoordinationSession
from agent_comms.coordination_tables.participants import Participants
from agent_comms.participant_store import ParticipantStore
from agent_comms.registry_document import RegistryDocument
from agent_comms.thread_identity import AgentThreadRole, UserThreadRole
from agent_comms.thread_status import StoppedThreadStatus
from agent_comms.thread_text import GivenThreadText
from agent_comms.threads import Thread
from cutover.carry_incarnation_participants import IncarnationParticipants


def test_incarnation_has_one_participant_before_registration_returns(tmp_path):
    comms = wire(tmp_path / "wire")
    first = comms.registry.declare(Thread("person", frozenset(), str(tmp_path), role=UserThreadRole))
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
            comms.registry.declare(Thread("missing", frozenset(), str(tmp_path)))
    assert "missing" not in comms.registry.snapshot().threads


@pytest.mark.parametrize("existing_store", (False, True))
def test_carry_registers_all_legacy_incarnations_once(tmp_path, existing_store):
    comms = wire(tmp_path / "wire")
    legacy = RegistryDocument()
    for index, role in enumerate((AgentThreadRole, AgentThreadRole, UserThreadRole)):
        thread = Thread(f"legacy-{index}", frozenset(), str(tmp_path), role=role)
        legacy.prepare_declaration(thread, StoppedThreadStatus()).apply(legacy)
    # The old registry write did not declare participant rows.
    comms.registry.store.replace(legacy)
    wanted = frozenset(thread.incarnation.lookup for thread in comms.registry.snapshot().threads.values())
    path = CoordinationStore.path_in(comms.root)
    if existing_store:
        with CoordinationSession(str(path)) as session:
            ParticipantStore(session).register(sorted(wanted)[0], committed=True)
    step = IncarnationParticipants(comms.root, comms.root, tmp_path / "spill")
    before = path.read_bytes() if path.exists() else b""
    dry = step.carry(apply=False)
    assert dry.changed == len(wanted) - int(existing_store)
    assert dry.rewritten == ()
    assert (path.read_bytes() if path.exists() else b"") == before
    carried = step.carry(apply=True)
    assert carried.changed == dry.changed
    assert carried.rewritten == (path.relative_to(comms.root),)
    with CoordinationSession(str(path)) as session:
        assert {row.participant_lookup for row in Participants.select(session._connection)} == wanted
        for lookup in wanted:
            assert ParticipantStore(session).get(lookup).committed
    before = path.read_bytes()
    assert step.carry(apply=True).changed == 0
    assert path.read_bytes() == before


def test_missing_participant_reader_names_lookup(tmp_path):
    with CoordinationSession(str(CoordinationStore.path_in(tmp_path))) as session:
        with pytest.raises(ParticipantNotRegistered, match="absent-lookup"):
            ParticipantStore(session).get("absent-lookup")
