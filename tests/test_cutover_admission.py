"""Runtime rebuilds retain history without re-admitting pre-cutover inputs."""

import json

import pytest

from agent_comms.cohort_foreground import _accept_visible_initials
from agent_comms.cohort_schema import install_private_cohort_schema
from agent_comms.comms import Comms
from agent_comms.coordinated_runtime_schema import install_native_runtime_schema
from agent_comms.coordination_cohort import accept_initial_cohort, sealed_cohort_assignments
from agent_comms.coordination_errors import IdentityConflict
from agent_comms.coordination_response import install_private_response_schema
from agent_comms.coordinator import Coordination
from agent_comms.errors import RelationViolationError
from agent_comms.proven_source_coverage import SourceCoverage
from agent_comms.wake_candidate_index import WakeCandidateIndex
from test_private_human_ingress import _root


def test_reset_rebuild_and_reopen_never_readmit_old_pending_input(tmp_path):
    comms, old_store, root_id, lookups = _root(tmp_path)
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

    with Coordination(str(comms.root / "coordination.sqlite")) as store:
        install_private_cohort_schema(store)
        install_private_response_schema(store)
        install_native_runtime_schema(store)
        for name, lookup in lookups.items():
            store.participants.register(lookup, name, name, committed=True)
        for _ in range(2):
            reopened = Comms(comms.root)
            WakeCandidateIndex(reopened.bus).maintain(rebuild=True)
            assert (
                _accept_visible_initials(
                    reopened.bus, root_id, store, lookups["bob"], 0, owner_name="bob"
                )
                == old.seq
            )
            assert sealed_cohort_assignments(store, lookups["bob"]) == ()
            with pytest.raises(IdentityConflict, match="admission floor"):
                accept_initial_cohort(reopened.bus, root_id, old.seq, store)
            coverage = SourceCoverage(
                reopened.bus, store, wire_root_id=root_id, recipient_lookup=lookups["bob"]
            ).prefix()
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
        coverage = SourceCoverage(
            reopened.bus, store, wire_root_id=root_id, recipient_lookup=lookups["bob"]
        ).prefix()
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
