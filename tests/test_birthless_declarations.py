"""Only concrete birth-marker declarations decode; external identity spelling stays fixed."""

import runpy
from abc import abstractmethod
from types import SimpleNamespace

import pytest
from metaclass_registry import DuplicateRegistryKeyError

from agent_comms import thread_identity
from agent_comms.thread_identity import (
    CoreWatcherIncarnation,
    DeletedThreadIncarnation,
    RecordedThreadIncarnation,
    ThreadIncarnation,
    UnrecordedThreadIncarnation,
)


@pytest.mark.parametrize('birth,member,decoded_name', [
    (-2.0, CoreWatcherIncarnation, 'core_watcher'),
    (-1.0, DeletedThreadIncarnation, 'original'),
    (0.0, UnrecordedThreadIncarnation, 'original'),
    (1.0, RecordedThreadIncarnation, 'original'),
])
def test_external_birth_markers_survive_wire_and_cli_boundaries(birth, member, decoded_name):
    wire = {'name': 'original', 'created_at': birth}
    identity = ThreadIncarnation.from_wire(wire)
    assert type(identity) is member
    assert identity.to_wire() == {**wire, 'name': decoded_name}
    assert ThreadIncarnation.from_arguments(identity.cli_arguments) == identity
    renamed = identity.renamed('renamed')
    assert type(renamed) is member
    assert renamed.to_wire() == {**wire, 'name': 'renamed'}


@pytest.mark.parametrize('birth', [-3.0, float('inf'), float('nan')])
def test_unknown_nonrecorded_birth_cannot_create_a_live_identity(birth):
    with pytest.raises(ValueError):
        ThreadIncarnation.from_wire({'name': 'unknown', 'created_at': birth})


@pytest.fixture
def identity_family():
    # Execute the actual class declarations without changing the canonical registry.
    return SimpleNamespace(**runpy.run_path(thread_identity.__file__, run_name=thread_identity.__name__))


def test_new_concrete_marker_drives_the_existing_decoder(identity_family):
    class HistoricalMarker(identity_family.BirthlessIncarnation):
        WIRE_BIRTH = -91.0

        @property
        def birth_description(self) -> str:
            return 'historical marker'

    wire = {'name': 'historical', 'created_at': HistoricalMarker.WIRE_BIRTH}
    identity = identity_family.ThreadIncarnation.from_wire(wire)
    assert type(identity) is HistoricalMarker
    assert identity.to_wire() == wire
    assert identity_family.ThreadIncarnation.from_arguments(identity.cli_arguments) == identity
    with pytest.raises(ValueError, match='positive recorded birth'):
        identity.require_recorded()


def test_abstract_specialization_does_not_contest_an_existing_marker(identity_family):
    class UnfinishedDeleted(identity_family.DeletedThreadIncarnation):
        @abstractmethod
        def unfinished(self): ...

    with pytest.raises(TypeError, match='abstract'):
        UnfinishedDeleted('name')
    decoded = identity_family.ThreadIncarnation.from_wire({'name': 'name', 'created_at': -1.0})
    assert type(decoded) is identity_family.DeletedThreadIncarnation


def test_new_abstract_marker_is_not_a_decoder(identity_family):
    class UnfinishedMarker(identity_family.BirthlessIncarnation):
        WIRE_BIRTH = -92.0

    with pytest.raises(ValueError, match='positive recorded birth'):
        identity_family.ThreadIncarnation.from_wire({'name': 'name', 'created_at': UnfinishedMarker.WIRE_BIRTH})


def test_two_concrete_owners_cannot_claim_one_marker(identity_family):
    with pytest.raises(DuplicateRegistryKeyError):
        class CompetingDeleted(identity_family.DeletedThreadIncarnation):
            pass
