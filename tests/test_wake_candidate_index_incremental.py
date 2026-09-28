"""Deferred, bounded candidate indexing never becomes delivery/native authority."""

from __future__ import annotations

import json
import os
import sqlite3
import time
from pathlib import Path

import pytest

from agent_comms.bus_publication import stable_thread_lookup
from agent_comms.child_process import ProcessIdentity
from agent_comms.comms import Comms
from agent_comms.threads import Thread
from agent_comms.wake_candidate_index import (
    CandidateCatchUp,
    CandidatePage,
    CommittedAppendHint,
    ProjectionRebuildRequiredError,
    ProjectionUnavailableError,
    WakeCandidateIndex,
)

pytestmark = pytest.mark.skipif(os.name != "posix", reason="private bus requires POSIX")


@pytest.fixture(autouse=True)
def _manual_projection_only(monkeypatch: pytest.MonkeyPatch) -> None:
    # Isolate explicit fault/rebuild/catch-up steps from the production daemon.
    monkeypatch.setattr(
        "agent_comms.messaging.schedule_candidate_catchup", lambda *_: None
    )


def _fresh(tmp_path: Path, *, recipients: int = 1) -> tuple[Comms, WakeCandidateIndex, str, str]:
    tmp_path.mkdir(parents=True, exist_ok=True)
    root = tmp_path / "wire"
    root.mkdir(mode=0o700)
    comms = Comms(root, private_initial_writes=True)
    comms.threads.register(
        Thread(
            "sender",
            frozenset({"writer"}),
            str(tmp_path),
            process_identity=ProcessIdentity.capture(os.getpid()),
            created_at=1_700_010_000.0,
        )
    )
    for number in range(recipients):
        comms.threads.register(
            Thread(
                f"member{number:03}",
                frozenset({"cohort"}),
                str(tmp_path),
                process_identity=ProcessIdentity.capture(os.getpid()),
                created_at=1_700_010_100.0 + number,
            )
        )
    root_id = comms.messaging.initialize_private_initial_protocol()
    return comms, WakeCandidateIndex(comms.bus), root_id, stable_thread_lookup(1_700_010_100.0)


def _page(
    index: WakeCandidateIndex, root_id: str, lookup: str, through: int, *, after: int = 0
) -> CandidatePage:
    return index.page(
        root_id=root_id,
        recipient_lookup=lookup,
        after_seq=after,
        required_through_seq=through,
        limit=100,
    )


def test_hint_is_pure_and_crash_after_bus_before_wal_catches_up(tmp_path: Path) -> None:
    comms, index, root_id, lookup = _fresh(tmp_path)
    first = comms.messaging.send_initial_cohort("sender", "member000", "first")
    before = comms.bus.log.path.read_bytes()
    hint = index.notify_committed_append(root_id=root_id, through_seq=first.seq)
    assert hint == CommittedAppendHint(root_id, 1)
    assert comms.bus.log.path.read_bytes() == before and not index.path.exists()
    with pytest.raises(ProjectionRebuildRequiredError, match="initial build"):
        index.catch_up_committed_append(hint)
    with pytest.raises(ProjectionUnavailableError):
        _page(index, root_id, lookup, first.seq)
    # Simulate process loss of volatile hint; explicit fresh post-commit catch-up
    # from the canonical source recovers derived candidates, not native inputs.
    reopened = WakeCandidateIndex(comms.bus)
    outcome = reopened.catch_up_committed_append(hint, bootstrap_new=True)
    assert outcome == CandidateCatchUp(first.seq, True, False)
    assert [row.source_seq for row in _page(reopened, root_id, lookup, first.seq).entries] == [1]
    assert reopened.catch_up_committed_append(hint) == outcome  # no WAL rewrite/duplicate
    assert comms.bus.log.path.read_bytes() == before


def test_deferred_catch_up_refuses_zero_progress_budget_until_explicitly_enlarged(
    tmp_path: Path,
) -> None:
    comms, index, root_id, lookup = _fresh(tmp_path)
    message = comms.messaging.send_initial_cohort("sender", "member000", "x" * 4096)
    hint = index.notify_committed_append(root_id=root_id, through_seq=message.seq)
    for _ in range(2):
        with pytest.raises(ProjectionUnavailableError, match="no checkpoint progress"):
            index.catch_up_committed_append(hint, max_bytes=128, bootstrap_new=True)
    assert index._verified_checkpoint(root_id) == 0
    # The underlying explicit maintenance API keeps its configurable budget;
    # a human/bounded scheduler may choose a larger safe batch, never spin.
    assert index.catch_up_committed_append(hint, max_bytes=8192, bootstrap_new=True).caught_up
    assert [row.source_seq for row in _page(index, root_id, lookup, message.seq).entries] == [1]


def test_crash_after_wal_before_ack_is_idempotent_and_future_hint_refuses(tmp_path: Path) -> None:
    comms, index, root_id, lookup = _fresh(tmp_path)
    first = comms.messaging.send_initial_cohort("sender", "member000", "first")
    hint = index.notify_committed_append(root_id=root_id, through_seq=first.seq)
    assert index.catch_up_committed_append(hint, bootstrap_new=True).caught_up
    # A bus commit+index WAL commit may survive while the caller loses the ACK.
    reopened = WakeCandidateIndex(comms.bus)
    assert reopened.catch_up_committed_append(hint) == CandidateCatchUp(1, True, False)
    with pytest.raises(ProjectionUnavailableError, match="exceeds verified bus high-water"):
        reopened.catch_up_committed_append(
            reopened.notify_committed_append(root_id=root_id, through_seq=2)
        )
    assert [row.source_seq for row in _page(reopened, root_id, lookup, 1).entries] == [1]


@pytest.mark.parametrize("damage", ["truncate", "replace", "incomplete"])
def test_changed_bus_or_incomplete_tail_never_promotes_a_hint(tmp_path: Path, damage: str) -> None:
    comms, index, root_id, lookup = _fresh(tmp_path)
    first = comms.messaging.send_initial_cohort("sender", "member000", "first")
    assert index.catch_up_committed_append(
        index.notify_committed_append(root_id=root_id, through_seq=first.seq),
        bootstrap_new=True,
    ).caught_up
    second = comms.messaging.send_initial_cohort("sender", "member000", "second")
    path = comms.bus.log.path
    if damage == "truncate":
        path.write_bytes(b"")
    elif damage == "replace":
        replacement = path.with_name("replacement")
        replacement.write_bytes(path.read_bytes())
        replacement.chmod(0o600)
        os.replace(replacement, path)
    else:
        with path.open("ab") as stream:
            stream.write(b"{incomplete")
    with pytest.raises((ProjectionUnavailableError, ProjectionRebuildRequiredError)):
        index.catch_up_committed_append(
            index.notify_committed_append(root_id=root_id, through_seq=second.seq)
        )
    with pytest.raises((ProjectionUnavailableError, ProjectionRebuildRequiredError)):
        _page(index, root_id, lookup, second.seq)


def test_foreign_root_is_rejected(tmp_path: Path) -> None:
    comms, index, root_id, lookup = _fresh(tmp_path / "one")
    first = comms.messaging.send_initial_cohort("sender", "member000", "first")
    hint = index.notify_committed_append(root_id=root_id, through_seq=first.seq)
    assert index.catch_up_committed_append(hint, bootstrap_new=True).caught_up
    other, other_index, other_id, _ = _fresh(tmp_path / "other")
    other.messaging.send_initial_cohort("sender", "member000", "elsewhere")
    with pytest.raises(ProjectionRebuildRequiredError, match="another root"):
        index.catch_up_committed_append(
            other_index.notify_committed_append(root_id=other_id, through_seq=1)
        )
    assert not other_index.path.exists()
    with pytest.raises(ValueError):
        index.notify_committed_append(root_id=root_id, through_seq=True)
    with pytest.raises(ValueError, match="exact append hint"):
        index.catch_up_committed_append(CommittedAppendHint(root_id, 0))


def test_reader_snapshot_does_not_block_deferred_wal_catch_up(tmp_path: Path) -> None:
    comms, index, root_id, lookup = _fresh(tmp_path)
    first = comms.messaging.send_initial_cohort("sender", "member000", "first")
    assert index.catch_up_committed_append(
        index.notify_committed_append(root_id=root_id, through_seq=first.seq),
        bootstrap_new=True,
    ).caught_up
    second = comms.messaging.send_initial_cohort("sender", "member000", "second")
    with sqlite3.connect(index.path, timeout=0.05) as reader:
        reader.execute("BEGIN")
        assert reader.execute("SELECT last_seq FROM candidate_checkpoint").fetchone() == (1,)
        assert index.catch_up_committed_append(
            index.notify_committed_append(root_id=root_id, through_seq=second.seq)
        ).caught_up
        assert reader.execute("SELECT last_seq FROM candidate_checkpoint").fetchone() == (1,)
        assert [row.source_seq for row in _page(index, root_id, lookup, 2).entries] == [1, 2]


def test_bounded_101_initials_and_150_frozen_recipients(tmp_path: Path) -> None:
    comms, index, root_id, lookup = _fresh(tmp_path, recipients=150)
    send_times: list[float] = []
    for number in range(101):
        start = time.perf_counter()
        message = comms.messaging.send_initial_cohort(
            "sender", "#cohort", f"@member000 benchmark {number}"
        )
        send_times.append(time.perf_counter() - start)
    hint = index.notify_committed_append(root_id=root_id, through_seq=message.seq)
    rounds = 0
    builds: list[float] = []
    while True:
        rounds += 1
        assert rounds <= 32  # finite event-driven continuation, no polling/sleep
        start = time.perf_counter()
        state = index.catch_up_committed_append(
            hint, bootstrap_new=rounds == 1, max_rows=256, max_bytes=8 * 1024 * 1024
        )
        builds.append(time.perf_counter() - start)
        if state.caught_up:
            break
        assert state.more_source_bytes
    pages: list[float] = []
    for _ in range(8):
        start = time.perf_counter()
        page = _page(index, root_id, lookup, message.seq)
        pages.append(time.perf_counter() - start)
        assert len(page.entries) == 100 and page.has_more
    final = _page(index, root_id, lookup, message.seq, after=100)
    assert [row.source_seq for row in final.entries] == [101]

    def p99(samples: list[float]) -> float:
        return round(sorted(samples)[int((len(samples) - 1) * 0.99)] * 1000, 3)

    # Only producer + candidate WAL timing; no injected supplement exists here.
    print(
        json.dumps(
            {
                "recipients": 150,
                "initials": 101,
                "rounds": rounds,
                "bus_bytes": comms.bus.log.path.stat().st_size,
                "send_p99_ms": p99(send_times),
                "candidate_batch_p99_ms": p99(builds),
                "candidate_page_p99_ms": p99(pages),
            }
        )
    )


def test_damaged_schema_requires_external_reset_without_repair(tmp_path):
    comms, index, root_id, lookup = _fresh(tmp_path)
    message = comms.messaging.send_initial_cohort("sender", "member000", "keep checkpoint")
    hint = index.notify_committed_append(root_id=root_id, through_seq=message.seq)
    assert index.catch_up_committed_append(hint, bootstrap_new=True).caught_up
    with sqlite3.connect(index.path) as db:
        db.execute("DROP TABLE candidate_response_key")
        saved = db.execute("SELECT * FROM candidate_checkpoint").fetchall()
    for rebuild in (False, True):
        with pytest.raises(ProjectionRebuildRequiredError, match="schema changed"):
            index.maintain(rebuild=rebuild)
    with sqlite3.connect(index.path) as db:
        assert db.execute("SELECT * FROM candidate_checkpoint").fetchall() == saved
        assert db.execute("SELECT count(*) FROM candidate").fetchone()[0] == 1
        assert (
            db.execute(
                "SELECT name FROM sqlite_master WHERE name='candidate_response_key'"
            ).fetchone()
            is None
        )
