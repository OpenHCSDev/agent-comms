"""Bounded, disposable N/K candidate pages are never claim_scope evidences."""

from __future__ import annotations

from conftest import publish_initial
import json
import multiprocessing
import os
import sqlite3
import sys
from pathlib import Path

import pytest

from agent_comms.thread_identity import RecordedThreadIncarnation
from agent_comms.bus_publication import (
    PRIVATE_WIRE_FIELD,
    public_envelope_digest,
)
from agent_comms.child_process import ProcessIdentity
from agent_comms.comms import Comms
from agent_comms.coordination_cohort import accept_delivery_cohort, sealed_cohort_assignments
from agent_comms.coordination_tables.publications import canonical_publication_key
from agent_comms.coordinator import Coordination
from agent_comms.errors import RelationViolationError
from agent_comms.private_path import FileRevision
from agent_comms.wire_log import SavedPrefixOpening
from agent_comms.wire_record import WireRecord
from agent_comms.threads import Thread
from agent_comms.typed_table import TypedTable
from agent_comms.wake_candidate_index import (
    CandidateCheckpoint, CandidateTable, ProjectionRebuildRequiredError,
    ProjectionUnavailableError, WakeCandidateIndex, FirstBuild, RebuildIndex, ResumeIndex,
)
from agent_comms.wake_policy import FullWake

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="private N/K bus requires POSIX")


@pytest.fixture(autouse=True)
def _manual_projection_only(monkeypatch: pytest.MonkeyPatch) -> None:
    # These tests own the explicit WAL maintenance schedule and fault points;
    # the production post-commit worker has separate integration coverage.
    monkeypatch.setattr(
        "agent_comms.candidate_maintenance.schedule_candidate_catchup", lambda *_: None
    )


def _maintain_in_process(root: str, root_id: str, through_bytes: int, start, results) -> None:
    """One independent runtime connection bootstrapping the shared WAL."""
    start.wait()
    try:
        index = WakeCandidateIndex(Comms(Path(root)).bus.log)
        hint = index.notify_committed_append(root_id=root_id, through_bytes=through_bytes)
        while True:
            outcome = index.catch_up_committed_append(
                hint, start=FirstBuild(), max_rows=256, max_bytes=8 * 1024 * 1024,
            )
            if outcome.caught_up:
                break
    except BaseException as error:
        results.put(("error", type(error).__name__, str(error)))
    else:
        results.put(("ok", outcome.caught_up))


@pytest.fixture
def participants(private_comms, tmp_path: Path) -> dict[str, str]:
    """This file's three team members on the shared private bus, by lookup."""
    created = {"sender": 17001.0, "Alice": 17002.0, "Bob": 17003.0}
    for name, identity in created.items():
        private_comms.registry.declare(
            private_comms.registry.new_thread(name, frozenset({'team'}), str(tmp_path), process_identity=ProcessIdentity.capture(os.getpid()), created_at=identity)
        )
    return {name: RecordedThreadIncarnation("fixture", identity).lookup for name, identity in created.items()}


def test_unbuilt_index_rejects_pages_and_resume_without_creating_storage(private_comms, participants):
    comms, lookup = private_comms, participants
    root_id = private_comms.bus.log.read_metadata_unlocked().wire_root_id
    message = publish_initial(comms, "sender", "Alice", "not indexed")
    index = WakeCandidateIndex(comms.bus.log)
    with pytest.raises(ProjectionUnavailableError, match="initial build"):
        index.page(root_id=root_id, recipient_lookup=lookup["Alice"], after_seq=0,
                   required_through_seq=message.seq)
    with pytest.raises(ProjectionRebuildRequiredError, match="initial build"):
        index._verified_checkpoint(root_id)
    hint = index.notify_committed_append(root_id=root_id,
                                        through_bytes=comms.bus.log.path.stat().st_size)
    with pytest.raises(ProjectionRebuildRequiredError, match="initial build"):
        index.catch_up_committed_append(hint)
    assert not index.path.exists()
    assert not index.path.with_name(f".{index.filename}.lock").exists()


def test_two_processes_serialize_checkpoint_replay_and_commit(tmp_path: Path, private_comms, participants) -> None:
    comms = private_comms
    root_id = private_comms.bus.log.read_metadata_unlocked().wire_root_id
    index = WakeCandidateIndex(comms.bus.log)
    publish_initial(comms, "sender", "#team", "initial")
    for number in range(128):
        publish_initial(comms, 
            "sender", "#team", f"{number:04d} " + "x" * 4096
        )
    expected_seq = comms.bus.log.latest_sequence()
    expected_bytes = comms.bus.log.path.stat().st_size
    context = multiprocessing.get_context("spawn")
    start, results = context.Event(), context.Queue()
    processes = [
        context.Process(
            target=_maintain_in_process,
            args=(str(comms.root), root_id, expected_bytes, start, results),
        )
        for _ in range(2)
    ]
    for process in processes:
        process.start()
    start.set()
    for process in processes:
        process.join(timeout=15)
        assert process.exitcode == 0
    outcomes = [results.get(timeout=2) for _ in processes]
    assert all(outcome[0] == "ok" for outcome in outcomes), outcomes
    with sqlite3.connect(index.path) as db:
        checkpoint = db.execute(
            "SELECT last_seq, byte_offset FROM candidate_checkpoint WHERE singleton=1"
        ).fetchone()
        count, distinct_count = db.execute(
            "SELECT count(*), count(DISTINCT source_seq || ':' || recipient_lookup) "
            "FROM candidate"
        ).fetchone()
    assert checkpoint == (expected_seq, expected_bytes)
    assert count == distinct_count


def test_malformed_checkpoint_keeps_its_rebuild_boundary(tmp_path, private_comms, participants):
    comms = private_comms
    root_id = private_comms.bus.log.read_metadata_unlocked().wire_root_id
    lookup = participants
    message = publish_initial(comms, 'sender', 'Alice', 'fixture')
    index = WakeCandidateIndex(comms.bus.log)
    assert index.maintain(start=FirstBuild())
    with sqlite3.connect(index.path) as db:
        # Deliberate index corruption, not a normal writer: STRICT protects
        # storage types and the version CHECK must be bypassed to inject it.
        db.execute("PRAGMA ignore_check_constraints=ON")
        db.execute("UPDATE candidate_checkpoint SET version=4")
    with pytest.raises(ProjectionRebuildRequiredError):
        index.page(root_id=root_id, recipient_lookup=lookup['Alice'], after_seq=0,
                   required_through_seq=message.seq)
    hint = index.notify_committed_append(root_id=root_id,
                                        through_bytes=comms.bus.log.path.stat().st_size)
    with pytest.raises(ProjectionRebuildRequiredError):
        index.catch_up_committed_append(hint)


def test_verified_replacement_rebuilds_inside_the_index_read(private_comms, participants):
    comms = private_comms
    root_id = comms.bus.log.read_metadata_unlocked().wire_root_id
    message = publish_initial(comms, "sender", "Alice", "original committed message")
    index = WakeCandidateIndex(comms.bus.log)
    assert index.maintain(start=FirstBuild())
    source = comms.bus.log.path
    replacement = source.with_name("replacement.jsonl")
    replacement.write_bytes(source.read_bytes())
    replacement.chmod(0o600)
    replacement.replace(source)
    # Source authority is still the canonical wire owner, not this projection.
    with comms.bus.log.checked_read() as checked:
        checked.require_current()
    page = index.page(root_id=root_id, recipient_lookup=participants["Alice"],
                      after_seq=0, required_through_seq=message.seq)
    assert [entry.source_seq for entry in page.entries] == [message.seq]
    assert index._verified_checkpoint(root_id).byte_offset == source.stat().st_size


def test_existing_candidate_schema_reopens_in_wal_through_the_store_owner(tmp_path, private_comms, participants):
    comms = private_comms
    index = WakeCandidateIndex(comms.bus.log)
    assert index.maintain(start=FirstBuild())
    with sqlite3.connect(index.path) as db:
        assert db.execute("PRAGMA journal_mode=DELETE").fetchone()[0] == "delete"
    assert index.maintain()
    with index.snapshot() as db:
        assert db.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert db.execute("SELECT version FROM candidate_checkpoint").fetchone()[0] == 3


def test_selected_candidates_are_not_sealed_work_and_no_wake_is_delivery_only(
    tmp_path: Path, private_comms, participants,
) -> None:
    comms = private_comms
    root_id = private_comms.bus.log.read_metadata_unlocked().wire_root_id
    lookup = participants
    message = publish_initial(comms, "sender", "#team", "@Alice investigate")
    index = WakeCandidateIndex(comms.bus.log)
    with pytest.raises(ProjectionUnavailableError):
        index.page(
            root_id=root_id,
            recipient_lookup=lookup["Alice"],
            after_seq=0,
            required_through_seq=message.seq,
        )
    assert index.maintain()  # The empty checkpoint selects its initial maintenance build.
    with sqlite3.connect(index.path) as db:
        assert db.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert db.execute("SELECT version FROM candidate_checkpoint").fetchone()[0] == 3
    selected = index.page(
        root_id=root_id,
        recipient_lookup=lookup["Alice"],
        after_seq=0,
        required_through_seq=message.seq,
    )
    assert len(selected.entries) == 1
    assert selected.entries[0].wake_mode is FullWake
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
    # Passive deliveries stay stored but are not part of the wake page.
    with sqlite3.connect(index.path) as db:
        assert db.execute(
            "SELECT count(*) FROM candidate WHERE recipient_lookup=? AND wake_mode IS NULL",
            (lookup["Bob"],),
        ).fetchone() == (1,)
    # Bus projection is only a candidate: coordinator has not sealed ANY wake.
    with Coordination(str(comms.root / "coordination.sqlite3")) as store:
        for name in ("Alice", "Bob"):
            store.participants.register(lookup[name], name, name, committed=True)
        assert sealed_cohort_assignments(store, lookup["Alice"]) == ()
        accept_delivery_cohort(comms.bus, root_id, message.seq, store)
        assert len(sealed_cohort_assignments(store, lookup["Alice"])) == 1
        assert sealed_cohort_assignments(store, lookup["Bob"]) == ()
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


@pytest.mark.parametrize(
    ("limits", "expected"),
    [
        ({"max_rows": True}, "max_rows"),
        ({"max_rows": 0}, "max_rows"),
        ({"max_rows": 257}, "max_rows"),
        ({"max_bytes": True}, "max_bytes"),
        ({"max_bytes": 0}, "max_bytes"),
        ({"max_bytes": 8 * 1024 * 1024 + 1}, "max_bytes"),
    ],
)
def test_maintenance_limits_fail_before_schema_or_checkpoint(
    tmp_path: Path, limits: dict[str, object], expected: str
, private_comms, participants) -> None:
    comms = private_comms
    index = WakeCandidateIndex(comms.bus.log)
    with pytest.raises(ValueError, match=expected):
        index.maintain(**limits)
    assert not index.path.exists()


def test_bounded_maintenance_replays_append_without_duplicate_or_cursor(tmp_path: Path, private_comms, participants) -> None:
    comms = private_comms
    root_id = private_comms.bus.log.read_metadata_unlocked().wire_root_id
    lookup = participants
    first = publish_initial(comms, "sender", "Alice", "first")
    second = publish_initial(comms, "sender", "Alice", "second")
    index = WakeCandidateIndex(comms.bus.log)
    assert not index.maintain(start=RebuildIndex(), max_rows=1)
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


def test_byte_budget_never_publishes_a_partial_candidate(tmp_path: Path, private_comms, participants) -> None:
    comms = private_comms
    root_id = private_comms.bus.log.read_metadata_unlocked().wire_root_id
    lookup = participants
    message = publish_initial(comms, "sender", "Alice", "a" * 4096)
    index = WakeCandidateIndex(comms.bus.log)
    # A batch budget smaller than the next record admits that one record
    # alone; it never publishes a partial candidate.
    assert index.maintain(start=RebuildIndex(), max_bytes=128)
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
    path = comms.bus.log.path
    path.write_text("".join(json.dumps(row) + "\n" for row in rows))
    path.chmod(0o600)


def test_invalid_intervening_response_blocks_later_candidate(tmp_path: Path, private_comms, participants) -> None:
    comms = private_comms
    root_id = private_comms.bus.log.read_metadata_unlocked().wire_root_id
    lookup = participants
    messages = [
        publish_initial(comms, "sender", "Alice", f"message {number}")
        for number in (1, 2, 3)
    ]
    rows = [json.loads(raw) for raw in comms.bus.log.path.read_text().splitlines()]
    rows[1][PRIVATE_WIRE_FIELD] = {"version": 1, "response": {}}
    _replace_rows(comms, rows)
    index = WakeCandidateIndex(comms.bus.log)
    with pytest.raises(ProjectionUnavailableError, match="candidate index maintenance unavailable"):
        index.maintain(start=RebuildIndex())
    with pytest.raises(ProjectionUnavailableError):
        index.page(
            root_id=root_id,
            recipient_lookup=lookup["Alice"],
            after_seq=0,
            required_through_seq=messages[2].seq,
        )
    with pytest.raises(
        RelationViolationError, match="Private bus checkpoint (root/size|prefix tail) changed"
    ):
        comms.bus.log.read_delivery_cohort(root_id, messages[2].seq)


@pytest.mark.parametrize(
    ("corrupt", "expected"),
    [
        ("wire_root_id", "another wire root"),
        ("envelope_digest", "envelope digest differs"),
        ("publication_key", "execution/route"),
    ],
)
def test_response_identity_must_match_private_bus_before_later_candidate(
    tmp_path: Path, corrupt: str, expected: str
, private_comms, participants) -> None:
    comms = private_comms
    root_id = private_comms.bus.log.read_metadata_unlocked().wire_root_id
    lookup = participants
    messages = [
        publish_initial(comms, "sender", "Alice", f"message {number}")
        for number in (1, 2, 3)
    ]
    rows = [json.loads(raw) for raw in comms.bus.log.path.read_text().splitlines()]
    rows[1] = _as_response(rows[1], root_id, "one-execution")
    response = rows[1][PRIVATE_WIRE_FIELD]["response"]
    response[corrupt] = "0" * 64 if corrupt == "envelope_digest" else "wrong"
    _replace_rows(comms, rows)
    index = WakeCandidateIndex(comms.bus.log)
    with pytest.raises(ProjectionUnavailableError) as failure:
        index.maintain(start=RebuildIndex())
    assert isinstance(failure.value.__cause__, RelationViolationError)
    # The source owner refuses the rewritten cut before candidate decoding.
    # Exercise the candidate decoder's separate record invariant directly.
    with pytest.raises(ValueError, match=expected):
        index._parse_row(rows[1], root_id, messages[0].seq)
    with pytest.raises(ProjectionUnavailableError):
        index.page(
            root_id=root_id,
            recipient_lookup=lookup["Alice"],
            after_seq=0,
            required_through_seq=messages[2].seq,
        )
    with pytest.raises(
        RelationViolationError, match="Private bus checkpoint (root/size|prefix tail) changed"
    ):
        comms.bus.log.read_delivery_cohort(root_id, messages[2].seq)


@pytest.mark.parametrize("first_batch_rows", [2, 4], ids=["stored-key", "same-batch"])
def test_duplicate_private_response_key_rejected_by_unique_constraint(
    tmp_path: Path, first_batch_rows: int
, private_comms, participants) -> None:
    """Exercise decoder/transaction invariants, not check a rewritten bus."""
    comms = private_comms
    root_id = private_comms.bus.log.read_metadata_unlocked().wire_root_id
    lookup = participants
    messages = [
        publish_initial(comms, "sender", "Alice", f"message {number}")
        for number in (1, 2, 3, 4)
    ]
    rows = [json.loads(raw) for raw in comms.bus.log.path.read_text().splitlines()]
    rows[1:3] = [_as_response(row, root_id, "duplicate-execution") for row in rows[1:3]]
    _replace_rows(comms, rows)
    index = WakeCandidateIndex(comms.bus.log)
    with comms.bus.log.opened_wire_snapshot(opening=SavedPrefixOpening()) as source:
        stat = os.fstat(source.stream.fileno())
        first_batch = index._replay_prefix(
            source, 0, 0, max_rows=first_batch_rows, max_bytes=256 * 1024
        )
        if first_batch_rows == 4:
            with (
                pytest.raises(ProjectionUnavailableError,
                              match="duplicate private response publication key"),
                index.writing() as db,
            ):
                index._commit_batch(
                    db, first_batch, CandidateCheckpoint.initial(root_id, source.revision),
                    index._source_end(source, first_batch.next_offset), start=RebuildIndex(),
                )
            with index.snapshot() as db:
                assert db.execute("SELECT count(*) FROM candidate").fetchone()[0] == 0
        else:
            with index.writing() as db:
                index._commit_batch(
                    db, first_batch, CandidateCheckpoint.initial(root_id, source.revision),
                    index._source_end(source, first_batch.next_offset), start=RebuildIndex(),
                )
            first = index.page(root_id=root_id, recipient_lookup=lookup["Alice"],
                               after_seq=0, required_through_seq=messages[1].seq)
            assert [candidate.source_seq for candidate in first.entries] == [
                m.seq for m in messages[:2]
            ]
            with index.snapshot() as db:
                saved = {
                    owner.declared_name: db.execute(f'SELECT * FROM "{owner.declared_name}"').fetchall()
                    for owner in TypedTable.members_with(CandidateTable)
                }
            second_batch = index._replay_prefix(
                source, first_batch.next_offset, first_batch.last_seq,
                max_rows=2, max_bytes=256 * 1024,
            )
            with (
                pytest.raises(ProjectionUnavailableError,
                              match="duplicate private response publication key"),
                index.writing() as db,
            ):
                index._commit_batch(
                    db, second_batch, CandidateCheckpoint.current(db).require_source(source, root_id),
                    index._source_end(source, second_batch.next_offset), start=ResumeIndex(),
                )
            with index.snapshot() as db:
                assert all(db.execute(f'SELECT * FROM "{table}"').fetchall() == values
                           for table, values in saved.items())
    with pytest.raises(ProjectionUnavailableError):
        index.page(root_id=root_id, recipient_lookup=lookup["Alice"],
                   after_seq=0, required_through_seq=messages[3].seq)
    # Even explicit index rebuilding cannot authorize this rewritten source.
    with pytest.raises(ProjectionUnavailableError):
        index.maintain(start=RebuildIndex())
    with pytest.raises(RelationViolationError):
        comms.bus.log.read_delivery_cohort(root_id, messages[3].seq)


def test_candidate_decode_releases_writer_and_discards_a_losing_batch(tmp_path, monkeypatch, private_comms, participants):
    comms = private_comms
    root_id = private_comms.bus.log.read_metadata_unlocked().wire_root_id
    lookup = participants
    messages = [publish_initial(comms, "sender", "Alice", body)
                for body in ("first", "second")]
    index = WakeCandidateIndex(comms.bus.log)
    replay = WakeCandidateIndex._replay_prefix
    races = []

    def another_writer(cls, source, *args, **kwargs):
        if not races:
            races.append(True)
            # This is a real second SQL writer, authorized while the outer
            # operation decodes. Its committed checkpoint must win intact.
            assert not index.maintain(start=FirstBuild(), max_rows=1)
        return replay(source, *args, **kwargs)

    monkeypatch.setattr(WakeCandidateIndex, "_replay_prefix", classmethod(another_writer))
    assert not index.maintain(start=RebuildIndex())
    assert index._verified_checkpoint(root_id).last_seq == messages[0].seq
    first = index.page(root_id=root_id, recipient_lookup=lookup["Alice"],
                       after_seq=0, required_through_seq=messages[0].seq)
    assert [entry.source_seq for entry in first.entries] == [messages[0].seq]
    assert index.maintain()
    completed = index.page(root_id=root_id, recipient_lookup=lookup["Alice"],
                           after_seq=0, required_through_seq=messages[-1].seq)
    assert [entry.source_seq for entry in completed.entries] == [m.seq for m in messages]


def test_maintenance_replays_its_captured_cut_not_a_later_partial_append(tmp_path, monkeypatch, private_comms, participants):
    comms = private_comms
    root_id = private_comms.bus.log.read_metadata_unlocked().wire_root_id
    lookup = participants
    message = publish_initial(comms, "sender", "Alice", "committed prefix")
    index = WakeCandidateIndex(comms.bus.log)
    replay = WakeCandidateIndex._replay_prefix
    cuts = []

    def interrupted_append(cls, source, *args, **kwargs):
        cuts.append(source.boundary)
        with comms.bus.log.path.open("ab") as output:
            output.write(b'{"pending":')
        return replay(source, *args, **kwargs)

    monkeypatch.setattr(WakeCandidateIndex, "_replay_prefix", classmethod(interrupted_append))
    assert index.maintain(start=RebuildIndex())
    assert index._verified_checkpoint(root_id).byte_offset == cuts[0]
    assert [entry.source_seq for entry in index.page(
        root_id=root_id, recipient_lookup=lookup["Alice"], after_seq=0,
        required_through_seq=message.seq,
    ).entries] == [message.seq]


def test_saved_candidate_prefix_ignores_later_tail_and_rebuilds_verified_replacement(tmp_path: Path, private_comms, participants) -> None:
    comms = private_comms
    root_id = private_comms.bus.log.read_metadata_unlocked().wire_root_id
    lookup = participants
    message = publish_initial(comms, "sender", "Alice", "first")
    index = WakeCandidateIndex(comms.bus.log)
    assert index.maintain(start=RebuildIndex())
    source = comms.bus.log.path
    with source.open("ab") as output:
        output.write(b'{"partial":')
    page = index.page(
        root_id=root_id,
        recipient_lookup=lookup["Alice"],
        after_seq=0,
        required_through_seq=message.seq,
    )
    assert [entry.source_seq for entry in page.entries] == [message.seq]
    assert index._verified_checkpoint(root_id).last_seq == message.seq
    # New preparation needs a committed source cut, unlike the saved-prefix
    # page above. The original source owner refuses this uncommitted tail.
    with pytest.raises(ProjectionUnavailableError):
        index.maintain()
    source.write_bytes(source.read_bytes().split(b"\n")[0] + b"\n")
    source.chmod(0o600)
    moved = source.with_name("replacement.jsonl")
    moved.write_bytes(source.read_bytes())
    moved.chmod(0o600)
    moved.replace(source)
    page = index.page(root_id=root_id, recipient_lookup=lookup["Alice"],
                      after_seq=0, required_through_seq=message.seq)
    assert [entry.source_seq for entry in page.entries] == [message.seq]
    assert index.maintain(start=ResumeIndex())
    # Rebuilding still borrows the canonical wire barrier's verified source.
    with comms.bus.log.checked_read() as checked:
        checked.require_current()
        assert checked.witness.revision == FileRevision.from_stat(source.stat())
    assert index._verified_checkpoint(root_id).device == source.stat().st_dev
    assert index._verified_checkpoint(root_id).inode == source.stat().st_ino


def _as_response(row, root_id, execution_id):
    """Build current declared response shape for raw-writer corruption tests."""
    from agent_comms.bus_publication import initial_sideband
    from agent_comms.delivery_policy import KeyedResponseAcknowledgement, ResponseDeliveryPolicy
    from agent_comms.field_codec import FieldCodec
    from agent_comms.wake import ControlClassification

    delivery = WireRecord.from_wire(row, root_id)
    public = delivery.message.to_wire()
    policy = ResponseDeliveryPolicy(
        version=1,
        initial=initial_sideband(
            root_id, delivery.message, delivery.audience,
            ResponseDeliveryPolicy.resolve(
                delivery.message, delivery.audience, ControlClassification.ORDINARY
            ),
            control=ControlClassification.ORDINARY.value,
        ),
        response=KeyedResponseAcknowledgement(
            root_id, execution_id,
            canonical_publication_key(execution_id, delivery.message.target),
            public_envelope_digest(public),
        ),
    )
    return {**public, PRIVATE_WIRE_FIELD: FieldCodec.encode(policy)}
