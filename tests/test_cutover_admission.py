"""Runtime rebuilds retain history without re-admitting pre-cutover inputs."""

import json

import pytest

from agent_comms.cohort_foreground import _accept_visible_initials
from agent_comms.cohort_schema import install_private_cohort_schema
from agent_comms.comms import Comms
from agent_comms.coordinated_runtime_schema import install_native_runtime_schema
from agent_comms.coordination_cohort import accept_initial_cohort, sealed_cohort_assignments
from agent_comms.coordination_response import install_private_response_schema
from agent_comms.coordination_store import IdentityConflict, MutationStore
from agent_comms.errors import RelationViolationError
from agent_comms.native_source_cursor import _bounded_coverage_pages
from agent_comms.private_bus_checkpoint import install_private_bus_checkpoint
from agent_comms.wake_candidate_index import WakeCandidateIndex
from test_private_human_ingress import _root


@pytest.mark.parametrize("certified", [False, True])
def test_reset_rebuild_and_reopen_never_readmit_old_pending_input(tmp_path, certified):
    comms, old_store, root_id, lookups = _root(tmp_path)
    if certified:
        comms.messaging.initialize_private_claim_protocol()
        install_private_bus_checkpoint(comms.bus.log)
    old = comms.messaging.send_user_message("bob", "old pending input", worktree=str(tmp_path))
    accept_initial_cohort(comms.bus, root_id, old.seq, old_store)
    assert len(sealed_cohort_assignments(old_store, lookups["bob"])) == 1
    old_store.close()
    # Preserve the actual old receipt database for history; rebuild only runtime.
    (comms.root / "coordination.sqlite").rename(comms.root / "prior-runtime.sqlite")
    with comms.bus.log.locked():
        marker = comms.bus.log._private_marker_unlocked()
        marker.admission_after_seq = marker.last_seq
        comms.bus.log.write_metadata_unlocked(marker)

    with MutationStore(str(comms.root / "coordination.sqlite")) as store:
        install_private_cohort_schema(store)
        install_private_response_schema(store)
        install_native_runtime_schema(store)
        for name, lookup in lookups.items():
            store.register_participant(lookup, name, name, committed=True)
        for _ in range(2):
            reopened = Comms(comms.root)
            WakeCandidateIndex(reopened.bus).maintain(rebuild=True)
            assert _accept_visible_initials(
                reopened.bus, root_id, store, lookups["bob"], 0, owner_name="bob"
            ) == old.seq
            assert sealed_cohort_assignments(store, lookups["bob"]) == ()
            with pytest.raises(IdentityConflict, match="admission floor"):
                accept_initial_cohort(reopened.bus, root_id, old.seq, store)
            coverage = _bounded_coverage_pages(reopened.bus, store, root_id, lookups["bob"])
            assert coverage.covered_seq == 0
            assert coverage.injected_source_seqs == ()

        fresh = reopened.messaging.send_user_message("bob", "fresh input", worktree=str(tmp_path))
        assert fresh.seq > old.seq
        with reopened.bus.log.locked():
            assert reopened.bus.log._private_marker_unlocked().admission_after_seq == old.seq
        for _ in range(2):
            _accept_visible_initials(
                reopened.bus, root_id, store, lookups["bob"], 0, owner_name="bob"
            )
        assignments = sealed_cohort_assignments(store, lookups["bob"])
        assert [assignment.wire_seq for assignment in assignments] == [fresh.seq]
        coverage = _bounded_coverage_pages(reopened.bus, store, root_id, lookups["bob"])
        assert coverage.covered_seq == 0
        assert coverage.injected_source_seqs == ()
        assert coverage.blocked_seq == fresh.seq  # Selection is not native proof.
        assert reopened.bus.log.full_history() == [old, fresh]


def test_persisted_marker_cannot_omit_admission_authority(tmp_path):
    comms, store, _root_id, _lookups = _root(tmp_path)
    store.close()
    path = comms.bus.log.metadata_path
    data = json.loads(path.read_text())
    del data["admission_after_seq"]
    path.write_text(json.dumps(data))
    with pytest.raises(RelationViolationError, match="required fields"), comms.bus.log.locked():
        comms.bus.log._private_marker_unlocked()


def test_archived_snapshot_preserves_rows_and_refuses_new_publication(tmp_path):
    source, store, _root_id, _lookups = _root(tmp_path)
    store.close()
    sent = source.messaging.send("alice", "bob", "retained message")
    reader = Comms(tmp_path / "reader")
    attached = reader.bus.attach_history(source.root)
    snapshot = Comms(attached.root)
    before = snapshot.bus.log.path.read_bytes()
    assert [message.message_id for message in snapshot.bus.log.full_history()] == [sent]
    with pytest.raises(RelationViolationError, match="read-only"):
        snapshot.messaging.send("alice", "bob", "must never append")
    assert snapshot.bus.log.path.read_bytes() == before
    attached.validate()
