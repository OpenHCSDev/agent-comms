"""OFF-only shared PR95 journal seam: no native pipe, ACP or provider use."""

import os
import sqlite3
from dataclasses import replace
from pathlib import Path

import pytest

from agent_comms.compaction_journal import (
    CompactionJournal,
    CompactionJournalError,
    CompactionJournalUnknownError,
    TypedRawInputWitness,
)
from agent_comms.private_sidecar import NativeRequestSource


def _setup(tmp_path: Path) -> tuple[CompactionJournal, Path]:
    root = tmp_path / "wire"
    root.mkdir()
    journal = CompactionJournal(root / "compaction.sqlite3")
    saved = root / "native-sessions" / "owner" / "saved.jsonl"
    saved.parent.mkdir(parents=True)
    fd = os.open(saved, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    os.close(fd)
    return journal, saved


def _reserve(journal: CompactionJournal, saved: Path, input_id: str = "a" * 32):
    return journal.reserve_typed_private_raw_input(
        saved,
        input_id,
        "bound prompt reply exactly",
        source_lane=NativeRequestSource.RPC,
        owner_name="owner",
        owner_generation=3,
        claim_id="claim",
        route_target="@owner",
        source_envelope_digest="b" * 64,
    )


def _check(journal: CompactionJournal, saved: Path, witness: TypedRawInputWitness) -> None:
    # No raw writer: entering this lock is solely a negative contract probe.
    with journal.ordinary_input_send_fence(
        saved, private_input_id=witness.input_id, typed_witness=witness
    ):
        pass


def test_old_unknown_remains_unmodified_permanent_typed_denial(tmp_path: Path) -> None:
    journal, saved = _setup(tmp_path)
    journal.reserve_private_raw_input(saved, "a" * 32)
    # Preserve frozen PR95's ID-only first-send semantics for an all-NULL
    # historic marker; entering the context does not write any raw byte.
    with journal.ordinary_input_send_fence(saved, private_input_id="a" * 32):
        pass
    with sqlite3.connect(journal.path) as db:
        before = db.execute("SELECT * FROM private_raw_inputs").fetchall()
        assert before == [("a" * 32, str(saved), "unknown", *([None] * 9))]
    # A caller-created dataclass, even with plausible identity, is no returned
    # post-fsync witness and cannot turn an old UNKNOWN into a typed marker.
    fake = TypedRawInputWitness(
        "a" * 32,
        str(saved),
        saved.stat().st_dev,
        saved.stat().st_ino,
        NativeRequestSource.RPC,
        "c" * 64,
        "owner",
        3,
        "claim",
        "@owner",
        "b" * 64,
    )
    with pytest.raises(CompactionJournalError, match="returned exact typed"):
        _check(journal, saved, fake)
    with pytest.raises(CompactionJournalError, match="already reserved"):
        _reserve(journal, saved)
    with sqlite3.connect(journal.path) as db:
        assert db.execute("SELECT * FROM private_raw_inputs").fetchall() == before


def test_frozen_three_column_raw_table_opens_additively_without_clearing_unknown(
    tmp_path: Path,
) -> None:
    root = tmp_path / "wire"
    root.mkdir()
    saved = root / "native-sessions" / "owner" / "saved.jsonl"
    saved.parent.mkdir(parents=True)
    saved.touch(mode=0o600)
    path = root / "compaction.sqlite3"
    with sqlite3.connect(path) as db:
        db.execute(
            "CREATE TABLE private_raw_inputs (input_id TEXT PRIMARY KEY, "
            "session_file TEXT NOT NULL, status TEXT NOT NULL CHECK(status = 'unknown'))"
        )
        db.execute(
            "INSERT INTO private_raw_inputs VALUES (?, ?, 'unknown')", ("a" * 32, str(saved))
        )
    journal = CompactionJournal(path)
    with sqlite3.connect(path) as db:
        row = db.execute("SELECT * FROM private_raw_inputs").fetchone()
        assert row == ("a" * 32, str(saved), "unknown", *([None] * 9))
    with pytest.raises(CompactionJournalError, match="already reserved"):
        _reserve(journal, saved)
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT * FROM private_raw_inputs").fetchone() == row


def test_typed_marker_mismatch_refuses_without_mutation_or_byte(tmp_path: Path) -> None:
    journal, saved = _setup(tmp_path)
    witness = _reserve(journal, saved)
    for changed in (
        replace(witness, source_lane=NativeRequestSource.INTERACTIVE),
        replace(witness, request_digest="c" * 64),
        replace(witness, owner_generation=4),
        replace(witness, claim_id="other"),
        replace(witness, route_target="@other"),
        replace(witness, source_envelope_digest="d" * 64),
    ):
        with pytest.raises(CompactionJournalError, match="returned exact typed"):
            _check(journal, saved, changed)
    with sqlite3.connect(journal.path) as db:
        before = db.execute("SELECT * FROM private_raw_inputs").fetchall()
    # Test-only hostile SQL tamper: even the originally returned object fails
    # when the marker no longer matches. This is not an external threat model.
    with sqlite3.connect(journal.path) as db:
        db.execute("UPDATE private_raw_inputs SET route_target='@other'")
    with pytest.raises(CompactionJournalError, match="source/claim/target/inode mismatch"):
        _check(journal, saved, witness)
    with sqlite3.connect(journal.path) as db:
        assert db.execute("SELECT * FROM private_raw_inputs").fetchone()[0:3] == before[0][0:3]
        assert db.execute("SELECT status FROM private_raw_inputs").fetchone() == ("unknown",)


@pytest.mark.parametrize(
    "missing_column",
    (
        "session_device",
        "session_inode",
        "source_lane",
        "request_digest",
        "owner_name",
        "owner_generation",
        "claim_id",
        "route_target",
        "source_envelope_digest",
    ),
)
def test_partially_null_typed_row_cannot_downgrade_to_id_only_fence(
    tmp_path: Path, missing_column: str
) -> None:
    journal, saved = _setup(tmp_path)
    witness = _reserve(journal, saved)
    with sqlite3.connect(journal.path) as db:
        # Disposable direct SQL mutation only: not external forgery authority.
        db.execute(f"UPDATE private_raw_inputs SET {missing_column}=NULL")
    with (
        pytest.raises(CompactionJournalError, match="Typed raw marker requires"),
        journal.ordinary_input_send_fence(saved, private_input_id=witness.input_id),
    ):
        pytest.fail("partially NULL typed row downgraded to ID-only fence")
    with pytest.raises(CompactionJournalError, match="source/claim/target/inode mismatch"):
        _check(journal, saved, witness)


def test_returned_marker_cannot_transfer_to_another_journal(tmp_path: Path) -> None:
    journal, saved = _setup(tmp_path)
    witness = _reserve(journal, saved)
    other = CompactionJournal(journal.path.with_name("other.sqlite3"))
    with sqlite3.connect(other.path) as db:
        db.execute("INSERT INTO private_raw_inputs VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", witness.row())
    with pytest.raises(CompactionJournalError, match="returned exact typed"):
        _check(other, saved, witness)
    _check(journal, saved, witness)  # failed cross-journal check did not consume it


def test_missing_or_replaced_inode_refuses_typed_final_check(tmp_path: Path) -> None:
    journal, saved = _setup(tmp_path)
    witness = _reserve(journal, saved)
    old_fd = os.open(saved, os.O_RDONLY)
    try:
        saved.unlink()
        with pytest.raises(CompactionJournalError, match="pinned saved session"):
            _check(journal, saved, witness)
        saved.write_bytes(b"replacement")
        saved.chmod(0o600)
        with pytest.raises(CompactionJournalError, match="source/claim/target/inode mismatch"):
            _check(journal, saved, witness)
    finally:
        os.close(old_fd)


def test_missing_and_linked_session_and_untyped_lane_never_create_marker(tmp_path: Path) -> None:
    journal, saved = _setup(tmp_path)
    missing = saved.with_name("missing.jsonl")
    with pytest.raises(CompactionJournalError, match="No pinned saved session inode"):
        _reserve(journal, missing)
    linked = saved.with_name("linked.jsonl")
    linked.symlink_to(saved)
    with pytest.raises(CompactionJournalError, match="traverses a link"):
        _reserve(journal, linked)
    with pytest.raises(CompactionJournalError, match="Typed RPC source"):
        journal.reserve_typed_private_raw_input(
            saved,
            "a" * 32,
            "text",
            source_lane="rpc",
            owner_name="owner",  # type: ignore[arg-type]
            owner_generation=3,
            claim_id="claim",
            route_target="@owner",
            source_envelope_digest="b" * 64,
        )
    with sqlite3.connect(journal.path) as db:
        assert db.execute("SELECT count(*) FROM private_raw_inputs").fetchone() == (0,)


def test_selected_row_denies_typed_reservation_before_marker(tmp_path: Path) -> None:
    journal, saved = _setup(tmp_path)
    with sqlite3.connect(journal.path) as db:
        db.execute(
            "INSERT INTO selected_summary_attempts "
            "(operation_id,session_file,source_json,status) VALUES (?,?,?,'unknown')",
            ("operation", str(saved), "{}"),
        )
    with pytest.raises(CompactionJournalError, match="Selected or unresolved journal"):
        _reserve(journal, saved)
    with sqlite3.connect(journal.path) as db:
        assert db.execute("SELECT count(*) FROM private_raw_inputs").fetchone() == (0,)


def test_parent_fsync_lost_ack_leaves_unknown_without_returned_witness(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    journal, saved = _setup(tmp_path)
    with monkeypatch.context() as scope:
        scope.setattr(os, "fsync", lambda _fd: (_ for _ in ()).throw(OSError("injected fsync")))
        with pytest.raises(CompactionJournalUnknownError, match="UNKNOWN"):
            _reserve(journal, saved)
    with sqlite3.connect(journal.path) as db:
        row = db.execute("SELECT input_id,status,source_lane FROM private_raw_inputs").fetchone()
        assert row == ("a" * 32, "unknown", "rpc")
    with pytest.raises(CompactionJournalError, match="already reserved"):
        _reserve(journal, saved)
    fake = TypedRawInputWitness(
        "a" * 32,
        str(saved),
        saved.stat().st_dev,
        saved.stat().st_ino,
        NativeRequestSource.RPC,
        "c" * 64,
        "owner",
        3,
        "claim",
        "@owner",
        "b" * 64,
    )
    with pytest.raises(CompactionJournalError, match="returned exact typed"):
        _check(journal, saved, fake)


def test_exact_returned_witness_is_single_use_and_new_input_remains_unknown(tmp_path: Path) -> None:
    journal, saved = _setup(tmp_path)
    first = _reserve(journal, saved)
    with (
        pytest.raises(CompactionJournalError, match="Typed raw marker requires"),
        journal.ordinary_input_send_fence(saved, private_input_id=first.input_id),
    ):
        pytest.fail("typed marker downgraded to ID-only fence")
    _check(journal, saved, first)  # no native byte is written
    with pytest.raises(CompactionJournalError, match="returned exact typed"):
        _check(journal, saved, first)
    second = _reserve(journal, saved, "c" * 32)
    assert second.input_id != first.input_id
    with sqlite3.connect(journal.path) as db:
        assert db.execute(
            "SELECT count(*) FROM private_raw_inputs WHERE status='unknown'"
        ).fetchone() == (2,)
    with pytest.raises(CompactionJournalError, match="returned exact typed"):
        _check(journal, saved, replace(second, input_id=first.input_id))
