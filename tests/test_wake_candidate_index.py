"""Bounded, disposable N/K candidate pages are never admission receipts."""

from __future__ import annotations

import json
import os
import sqlite3
import sys
from pathlib import Path

import pytest

from agent_comms.bus_publication import (
    PRIVATE_WIRE_FIELD,
    public_envelope_digest,
    stable_thread_lookup,
)
from agent_comms.cohort_schema import install_private_cohort_schema
from agent_comms.coordination import canonical_publication_key
from agent_comms.coordination_cohort import accept_initial_cohort, sealed_cohort_claims
from agent_comms.coordination_store import MutationStore
from agent_comms.declarations import RelationViolationError, Thread
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
        assert db.execute("SELECT version FROM checkpoint").fetchone()[0] == 2
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


def _replace_rows(comms: Comms, rows: list[dict]) -> None:
    path = comms.bus._path
    path.write_text("".join(json.dumps(row) + "\n" for row in rows))
    path.chmod(0o600)


def test_invalid_intervening_response_blocks_later_candidate(tmp_path: Path) -> None:
    comms, root_id, lookup = _private(tmp_path)
    messages = [
        comms.send_initial_cohort("sender", "Alice", f"message {number}") for number in (1, 2, 3)
    ]
    rows = [json.loads(raw) for raw in comms.bus._path.read_text().splitlines()]
    rows[1][PRIVATE_WIRE_FIELD] = {"version": 1, "response": {}}
    _replace_rows(comms, rows)
    index = WakeCandidateIndex(comms.bus)
    with pytest.raises(ProjectionUnavailableError, match="malformed candidate response"):
        index.maintain(rebuild=True)
    with pytest.raises(ProjectionUnavailableError):
        index.page(
            root_id=root_id,
            recipient_lookup=lookup["Alice"],
            after_seq=0,
            required_through_seq=messages[2].seq,
        )
    with pytest.raises(RelationViolationError, match="malformed private bus receipt"):
        comms.bus.read_initial_cohort(root_id, messages[2].seq)


@pytest.mark.parametrize("corrupt", ["wire_root_id", "envelope_digest", "publication_key"])
def test_response_identity_must_match_private_bus_before_later_candidate(
    tmp_path: Path, corrupt: str
) -> None:
    comms, root_id, lookup = _private(tmp_path)
    messages = [
        comms.send_initial_cohort("sender", "Alice", f"message {number}") for number in (1, 2, 3)
    ]
    rows = [json.loads(raw) for raw in comms.bus._path.read_text().splitlines()]
    public = {key: value for key, value in rows[1].items() if key != PRIVATE_WIRE_FIELD}
    response = {
        "wire_root_id": root_id,
        "execution_id": "one-execution",
        "publication_key": canonical_publication_key("one-execution", "Alice"),
        "envelope_digest": public_envelope_digest(public),
    }
    response[corrupt] = "0" * 64 if corrupt == "envelope_digest" else "wrong"
    rows[1][PRIVATE_WIRE_FIELD] = {"version": 1, "response": response}
    _replace_rows(comms, rows)
    index = WakeCandidateIndex(comms.bus)
    with pytest.raises(ProjectionUnavailableError, match="invalid or duplicate response"):
        index.maintain(rebuild=True)
    with pytest.raises(ProjectionUnavailableError):
        index.page(
            root_id=root_id,
            recipient_lookup=lookup["Alice"],
            after_seq=0,
            required_through_seq=messages[2].seq,
        )
    with pytest.raises(RelationViolationError, match="malformed private bus receipt"):
        comms.bus.read_initial_cohort(root_id, messages[2].seq)


def test_duplicate_private_response_key_fails_across_maintenance_batches(
    tmp_path: Path,
) -> None:
    comms, root_id, lookup = _private(tmp_path)
    messages = [
        comms.send_initial_cohort("sender", "Alice", f"message {number}") for number in (1, 2, 3, 4)
    ]
    rows = [json.loads(raw) for raw in comms.bus._path.read_text().splitlines()]
    for row in rows[1:3]:
        public = {key: value for key, value in row.items() if key != PRIVATE_WIRE_FIELD}
        row[PRIVATE_WIRE_FIELD] = {
            "version": 1,
            "response": {
                "wire_root_id": root_id,
                "execution_id": "duplicate-execution",
                "publication_key": canonical_publication_key("duplicate-execution", "Alice"),
                "envelope_digest": public_envelope_digest(public),
            },
        }
    _replace_rows(comms, rows)
    index = WakeCandidateIndex(comms.bus)
    assert not index.maintain(rebuild=True, max_rows=2)
    first = index.page(
        root_id=root_id,
        recipient_lookup=lookup["Alice"],
        after_seq=0,
        required_through_seq=messages[1].seq,
    )
    assert [candidate.source_seq for candidate in first.entries] == [messages[0].seq]
    with pytest.raises(ProjectionUnavailableError, match="duplicate response"):
        index.maintain(max_rows=2)
    with pytest.raises(ProjectionUnavailableError, match="stale"):
        index.page(
            root_id=root_id,
            recipient_lookup=lookup["Alice"],
            after_seq=0,
            required_through_seq=messages[3].seq,
        )
    with pytest.raises(RelationViolationError, match="malformed private bus receipt"):
        comms.bus.read_initial_cohort(root_id, messages[3].seq)


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
