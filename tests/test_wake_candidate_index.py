"""Bounded, disposable N/K candidate pages are never admission receipts."""

from __future__ import annotations

import os
import sqlite3
import sys
from pathlib import Path

import pytest

from agent_comms.bus_publication import stable_thread_lookup
from agent_comms.cohort_schema import install_private_cohort_schema
from agent_comms.coordination_cohort import accept_initial_cohort, sealed_cohort_claims
from agent_comms.coordination_store import MutationStore
from agent_comms.declarations import Thread
from agent_comms.operations import Comms
from agent_comms.wake_candidate_index import (
    ProjectionRebuildRequiredError,
    ProjectionUnavailableError,
    WakeCandidateIndex,
)

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="private N/K bus requires POSIX")


def _private(tmp_path: Path) -> tuple[Comms, str, dict[str, str]]:
    root = tmp_path / "wire"
    root.mkdir(mode=0o700)
    comms = Comms(root, private_initial_writes=True)
    created = {"sender": 17001.0, "Alice": 17002.0, "Bob": 17003.0}
    for name, identity in created.items():
        comms.register(
            Thread(name, frozenset({"team"}), str(tmp_path), pid=os.getpid(), created_at=identity)
        )
    root_id = comms.initialize_private_initial_protocol()
    lookup = {name: stable_thread_lookup(identity) for name, identity in created.items()}
    return comms, root_id, lookup


def test_selected_candidates_are_not_sealed_work_and_no_wake_is_delivery_only(
    tmp_path: Path,
) -> None:
    comms, root_id, lookup = _private(tmp_path)
    message = comms.send_initial_cohort("sender", "#team", "@Alice investigate")
    index = WakeCandidateIndex(comms.bus)
    with pytest.raises(ProjectionUnavailableError):
        index.page(
            root_id=root_id,
            recipient_lookup=lookup["Alice"],
            after_seq=0,
            required_through_seq=message.seq,
        )
    with pytest.raises(ProjectionRebuildRequiredError):
        index.maintain()  # Initial rebuild must be an explicit maintenance choice.
    assert index.maintain(rebuild=True)
    with sqlite3.connect(index.path) as db:
        assert db.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert db.execute("SELECT version FROM checkpoint").fetchone()[0] == 1
    selected = index.page(
        root_id=root_id,
        recipient_lookup=lookup["Alice"],
        after_seq=0,
        required_through_seq=message.seq,
    )
    assert len(selected.entries) == 1
    assert selected.entries[0].wake_mode == "full"
    assert selected.entries[0].source_seq == message.seq
    assert not selected.has_more
    assert (
        index.page(
            root_id=root_id,
            recipient_lookup=lookup["Bob"],
            after_seq=0,
            required_through_seq=message.seq,
        ).entries
        == ()
    )
    passive = index.page(
        root_id=root_id,
        recipient_lookup=lookup["Bob"],
        after_seq=0,
        required_through_seq=message.seq,
        delivery_only=True,
    )
    assert len(passive.entries) == 1 and passive.entries[0].wake_mode is None
    # Bus projection is only a candidate: coordinator has not sealed ANY wake.
    with MutationStore(str(comms.root / "coordination.sqlite3")) as store:
        install_private_cohort_schema(store)
        for name in ("Alice", "Bob"):
            store.register_participant(lookup[name], name, name, committed=True)
        assert sealed_cohort_claims(store, lookup["Alice"]) == ()
        accept_initial_cohort(comms.bus, root_id, message.seq, store)
        assert len(sealed_cohort_claims(store, lookup["Alice"])) == 1
        assert sealed_cohort_claims(store, lookup["Bob"]) == ()
    assert index.maintain()  # Exact no-new-bytes replay adds nothing.
    assert (
        index.page(
            root_id=root_id,
            recipient_lookup=lookup["Alice"],
            after_seq=0,
            required_through_seq=message.seq,
        )
        == selected
    )


def test_bounded_maintenance_replays_append_without_duplicate_or_cursor(tmp_path: Path) -> None:
    comms, root_id, lookup = _private(tmp_path)
    first = comms.send_initial_cohort("sender", "Alice", "first")
    second = comms.send_initial_cohort("sender", "Alice", "second")
    index = WakeCandidateIndex(comms.bus)
    assert not index.maintain(rebuild=True, max_rows=1)
    with pytest.raises(ProjectionUnavailableError, match="stale"):
        index.page(
            root_id=root_id,
            recipient_lookup=lookup["Alice"],
            after_seq=0,
            required_through_seq=second.seq,
        )
    assert index.maintain(max_rows=1)
    page = index.page(
        root_id=root_id,
        recipient_lookup=lookup["Alice"],
        after_seq=0,
        required_through_seq=second.seq,
        limit=1,
    )
    assert page.through_seq == second.seq and page.has_more
    assert [row.source_seq for row in page.entries] == [first.seq]
    assert [
        row.source_seq
        for row in index.page(
            root_id=root_id,
            recipient_lookup=lookup["Alice"],
            after_seq=first.seq,
            required_through_seq=second.seq,
        ).entries
    ] == [second.seq]
    assert index.maintain(max_rows=1)
    assert (
        len(
            index.page(
                root_id=root_id,
                recipient_lookup=lookup["Alice"],
                after_seq=0,
                required_through_seq=second.seq,
            ).entries
        )
        == 2
    )
    with pytest.raises(ProjectionUnavailableError, match="stale"):
        index.page(
            root_id=root_id,
            recipient_lookup=lookup["Alice"],
            after_seq=0,
            required_through_seq=second.seq + 1,
        )


def test_byte_budget_never_publishes_a_partial_candidate(tmp_path: Path) -> None:
    comms, root_id, lookup = _private(tmp_path)
    message = comms.send_initial_cohort("sender", "Alice", "a" * 4096)
    index = WakeCandidateIndex(comms.bus)
    assert not index.maintain(rebuild=True, max_bytes=128)
    with pytest.raises(ProjectionUnavailableError, match="stale"):
        index.page(
            root_id=root_id,
            recipient_lookup=lookup["Alice"],
            after_seq=0,
            required_through_seq=message.seq,
        )
    assert index.maintain(max_bytes=16 * 1024)
    assert (
        len(
            index.page(
                root_id=root_id,
                recipient_lookup=lookup["Alice"],
                after_seq=0,
                required_through_seq=message.seq,
            ).entries
        )
        == 1
    )


def test_rewrite_and_incomplete_tail_omit_optional_projection(tmp_path: Path) -> None:
    comms, root_id, lookup = _private(tmp_path)
    message = comms.send_initial_cohort("sender", "Alice", "first")
    index = WakeCandidateIndex(comms.bus)
    assert index.maintain(rebuild=True)
    source = comms.bus._path
    with source.open("ab") as output:
        output.write(b'{"partial":')
    with pytest.raises(ProjectionUnavailableError, match="incomplete tail"):
        index.page(
            root_id=root_id,
            recipient_lookup=lookup["Alice"],
            after_seq=0,
            required_through_seq=message.seq,
        )
    with pytest.raises(ProjectionUnavailableError, match="incomplete"):
        index.maintain()
    source.write_bytes(source.read_bytes().split(b"\n")[0] + b"\n")
    source.chmod(0o600)
    moved = source.with_name("replacement.jsonl")
    moved.write_bytes(source.read_bytes())
    moved.chmod(0o600)
    moved.replace(source)
    with pytest.raises(ProjectionRebuildRequiredError):
        index.page(
            root_id=root_id,
            recipient_lookup=lookup["Alice"],
            after_seq=0,
            required_through_seq=message.seq,
        )
    with pytest.raises(ProjectionRebuildRequiredError):
        index.maintain()
    assert index.maintain(rebuild=True)
    assert (
        len(
            index.page(
                root_id=root_id,
                recipient_lookup=lookup["Alice"],
                after_seq=0,
                required_through_seq=message.seq,
            ).entries
        )
        == 1
    )
