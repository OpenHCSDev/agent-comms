"""Release proof follows process identity, not unrelated thread metadata."""

from dataclasses import replace

from agent_comms.comms import wire
from agent_comms.owner_lifecycle import NoRelease, OwnerReleaseEvidence
from agent_comms.thread_text import GivenThreadText
from agent_comms.threads import Thread


def test_release_survives_metadata_changes_and_reads_only_its_row(tmp_path):
    comms = wire(tmp_path)
    originals = [comms.registry.declare(Thread(name, frozenset(), str(tmp_path)))
                 for name in ('worker', 'other')]
    generation = comms.registry.snapshot().claim_generation('worker')
    for thread in originals:
        comms.owners.stop(thread.name)
    snapshot = comms.registry.snapshot()
    current = snapshot.threads['worker']
    comms.registry.register(replace(current, title=GivenThreadText('changed title'), task=GivenThreadText('changed task')),
                            status=snapshot.statuses['worker'])
    releases = comms.owners.releases
    # An invalid unrelated row must not even be decoded by this lookup.
    with releases.writing() as db:
        OwnerReleaseEvidence.update(db, where='name=?', parameters=('other',), after=0)
    record = releases.get('worker')
    assert record.current(comms.registry.snapshot(), originals[0], generation)
    assert record.incarnation() == originals[0].incarnation
    assert record.process_identity == originals[0].process_identity
    releases.remove_threads((originals[0],))
    assert isinstance(releases.get('worker'), NoRelease)


def test_empty_release_store_is_not_proof(tmp_path):
    comms = wire(tmp_path)
    thread = comms.registry.declare(Thread('worker', frozenset(), str(tmp_path)))
    record = comms.owners.releases.get(thread.name)
    assert isinstance(record, NoRelease)
    assert not record.current(comms.registry.snapshot(), thread, 1)
    assert not record.matches(thread)
    with comms.owners.releases.snapshot() as db:
        assert OwnerReleaseEvidence.count(db) == 0
