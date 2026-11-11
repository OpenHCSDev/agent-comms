"""External birth markers decode to their declared member and keep their wire spelling."""

import pytest

from agent_comms.registry.thread_identity import (
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
