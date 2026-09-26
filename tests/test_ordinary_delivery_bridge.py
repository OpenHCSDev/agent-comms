"""Ordinary-delivery N/K candidate bridge: record-only semantics (no wake/claim)."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest

from agent_comms import Thread
from agent_comms.bus_publication import stable_thread_lookup
from agent_comms.cohort_schema import install_private_cohort_schema
from agent_comms.coordination_store import IdentityConflict, MutationStore
from agent_comms.operations import Comms
from agent_comms.ordinary_delivery_bridge import (
    install_ordinary_delivery_schema,
    read_ordinary_delivery_candidates,
    record_ordinary_delivery,
)


@pytest.fixture
def tmp_path():
    if os.name != "posix" or not Path("/var/tmp").is_dir() or Path("/var").is_symlink():
        pytest.skip("private roots require a real, disposable /var/tmp")
    with tempfile.TemporaryDirectory(prefix="ac-ordinary-test-", dir="/var/tmp") as root:
        yield Path(root)


def _root(tmp_path: Path):
    """Public wire for ordinary sends; separate private cohort root for the bridge."""
    root = tmp_path / "wire"
    root.mkdir(mode=0o700)
    comms = Comms(root)
    people = [
        Thread("sender", frozenset(), str(tmp_path), pid=os.getpid()),
        Thread("alpha", frozenset({"team"}), str(tmp_path), pid=os.getpid()),
    ]
    for person in people:
        comms.register(person)
    private = tmp_path / "private"
    private.mkdir(mode=0o700)
    private_root_id = "0" * 32
    with MutationStore(str(private / "coordination.sqlite3")) as store:
        install_private_cohort_schema(store)
        install_ordinary_delivery_schema(store)
        for person in people:
            store.register_participant(
                stable_thread_lookup(person.created_at),
                person.name,
                person.name,
                committed=True,
            )
    return root, private_root_id, comms, people, private


def test_ordinary_send_records_candidate_without_wake_or_claim(tmp_path: Path):
    root, root_id, comms, people, private = _root(tmp_path)
    ordinary = comms.send_message("sender", "#team", "A normal channel post")
    lookup = stable_thread_lookup(people[1].created_at)
    with MutationStore(str(private / "coordination.sqlite3")) as store:
        candidate = record_ordinary_delivery(
            store,
            comms,
            wire_root_id=root_id,
            wire_seq=ordinary.seq,
            recipient_lookups=(lookup,),
        )
        assert candidate.sender == "sender" and candidate.exact_target == "#team"
        assert candidate.recipients == (lookup,)
        # Idempotent for identical identity.
        again = record_ordinary_delivery(
            store,
            comms,
            wire_root_id=root_id,
            wire_seq=ordinary.seq,
            recipient_lookups=(lookup,),
        )
        assert again.envelope_digest == candidate.envelope_digest
        page = read_ordinary_delivery_candidates(store, recipient_lookup=lookup)
        assert len(page) == 1 and page[0].wire_seq == ordinary.seq
        page = read_ordinary_delivery_candidates(
            store, recipient_lookup=lookup, after_seq=ordinary.seq
        )
        assert page == ()
        # The candidate sidecar stays isolated from any claim authority; the
        # private store was installed without cohort claim content here, and
        # recording wrote nothing beyond its own derived rows.
        import agent_comms.ordinary_delivery_bridge as bridge

        with bridge.sidecar_connection(
            bridge.ordinary_delivery_store_path(store), bridge._DDL, bridge._DDL_DIGEST
        ) as db:
            kinds = db.execute("SELECT COUNT(*) c FROM ordinary_delivery_candidates").fetchone()
            assert kinds["c"] == 1


def test_uncommitted_or_missing_participant_refuses(tmp_path: Path):
    root, root_id, comms, people, private = _root(tmp_path)
    ordinary = comms.send_message("sender", "#team", "Another normal post")
    uncommitted = Thread("drifter", frozenset(), str(tmp_path), pid=os.getpid())
    comms.register(uncommitted)
    drift_lookup = stable_thread_lookup(uncommitted.created_at)
    with MutationStore(str(private / "coordination.sqlite3")) as store:
        store.register_participant(drift_lookup, "drifter", "drifter", committed=False)
        # An uncommitted participant and a completely missing lookup both refuse.
        for lookup in (drift_lookup, "f" * 32):
            with pytest.raises(
                IdentityConflict,
                match="committed participants|participant aggregate is not registered",
            ):
                record_ordinary_delivery(
                    store,
                    comms,
                    wire_root_id=root_id,
                    wire_seq=ordinary.seq,
                    recipient_lookups=(lookup,),
                )
        page = read_ordinary_delivery_candidates(
            store, recipient_lookup=stable_thread_lookup(people[1].created_at)
        )
        assert page == ()


def test_missing_committed_bus_row_refuses(tmp_path: Path):
    root, root_id, comms, people, private = _root(tmp_path)
    with (
        MutationStore(str(private / "coordination.sqlite3")) as store,
        pytest.raises(IdentityConflict, match="committed public bus row"),
    ):
        record_ordinary_delivery(
            store,
            comms,
            wire_root_id=root_id,
            wire_seq=10_000,
            recipient_lookups=(stable_thread_lookup(people[1].created_at),),
        )
