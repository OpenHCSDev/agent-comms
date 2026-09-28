"""Provider-free durable pre-send selected-summary operation IDs and input gate."""

import json
import os
import sqlite3
import subprocess
import sys
import threading
from pathlib import Path

import pytest

from agent_comms.compaction_journal import (
    CompactionJournal,
    CompactionJournalError,
    CompactionJournalUnknownError,
    SelectedSummaryAttempt,
)
from agent_comms.compaction_send_admission import native_input_admitted
from agent_comms.compaction_states import (
    AbortedNoWriteOperation,
    CommittedOperation,
    DeclinedPrestartSummary,
    ReservedSummary,
)
from agent_comms.input_disposition import InputDispositions
from selected_summary_cases import manual_source

pytestmark = pytest.mark.skipif(os.name != "posix", reason="POSIX durable journal")


@pytest.fixture
def reserved(tmp_path):
    session = tmp_path / "session.jsonl"
    session.write_text('{"type":"session","version":3}\n')
    journal = CompactionJournal(tmp_path / "compaction-commits.sqlite3")
    source = {
        "source": manual_source(session),
        "selected": {"provider": "fixture", "modelId": "model", "contextWindow": 1000},
        "settings": {"reserveTokens": 100, "keepRecentTokens": 100},
    }
    return journal, str(session), source


def test_reservation_is_durable_unresolved_and_blocks_every_input(reserved):
    journal, session, source = reserved
    operation_id = journal.reserve_selected_summary(session, source, operation_id="a" * 32)
    assert operation_id == "a" * 32
    reopened = CompactionJournal(journal.path)
    attempt = reopened.selected_summary(operation_id)
    assert attempt.state == ReservedSummary()
    assert json.loads(attempt.source_json) == source
    assert reopened.unresolved_selected_summary(session) == (attempt,)
    assert not native_input_admitted(journal.path.parent, session)
    with pytest.raises(CompactionJournalError, match="never replay"):
        reopened.reserve_selected_summary(session, source, operation_id=operation_id)
    with pytest.raises(CompactionJournalError, match="never replay"):
        reopened.reserve_selected_summary(session, source)
    reopened.mark_selected_summary_unknown(operation_id)
    assert reopened.selected_summary(operation_id).state.declared_name == "unknown"
    assert not native_input_admitted(journal.path.parent, session)
    with pytest.raises(CompactionJournalError, match="Exact committed"):
        reopened.link_selected_summary_commit(operation_id, "b" * 32)


def test_raw_send_fence_blocks_every_same_session_status_not_unrelated(reserved):
    journal, session, source = reserved
    other = Path(session).with_name("other.jsonl")
    other.write_text('{"type":"session","id":"other"}\n')
    with journal.ordinary_input_send_fence(Path(session)):
        pass
    operation_id = journal.reserve_selected_summary(session, source)
    for status in ("reserved", "unknown", "declined-prestart"):
        if status == "unknown":
            journal.mark_selected_summary_unknown(operation_id)
        elif status == "declined-prestart":
            # An apparent terminal row is still not ordinary send authority.
            with journal._transaction() as db:
                SelectedSummaryAttempt.update(
                    db,
                    where="operation_id=?",
                    parameters=(operation_id,),
                    state=DeclinedPrestartSummary("split_turn"),
                )
        with (
            pytest.raises(CompactionJournalError, match="blocks native input"),
            journal.ordinary_input_send_fence(Path(session)),
        ):
            raise AssertionError("same-session selected row must refuse prewrite")
        with journal.ordinary_input_send_fence(other):
            pass


def test_raw_send_fence_serializes_concurrent_direct_reservation(reserved):
    journal, session, source = reserved
    started, finished = threading.Event(), threading.Event()
    failures: list[BaseException] = []

    def reserve() -> None:
        started.set()
        try:
            journal.reserve_selected_summary(session, source)
        except BaseException as error:
            failures.append(error)
        finally:
            finished.set()

    worker = threading.Thread(target=reserve, daemon=True)
    with journal.ordinary_input_send_fence(Path(session)):
        worker.start()
        assert started.wait(2)
        assert not finished.wait(0.05), "reservation committed during raw-send exclusion"
    worker.join(timeout=3)
    assert finished.is_set() and not failures
    with (
        pytest.raises(CompactionJournalError, match="blocks native input"),
        journal.ordinary_input_send_fence(Path(session)),
    ):
        pass


def test_legacy_private_session_without_marker_fails_closed_at_selected_reserve(reserved):
    journal, session, source = reserved
    private_dir = journal.path.parent / "native-sessions" / ("f" * 32)
    private_dir.mkdir(parents=True)
    saved = private_dir / "old-pr94-session.jsonl"
    saved.write_text('{"type":"session","id":"legacy"}\n')
    # This file can predate marker deployment; absence of a marker never
    # proves it has no old PR94 raw or UNKNOWN input on the same session.
    with sqlite3.connect(journal.path) as db:
        assert db.execute("SELECT count(*) FROM private_raw_inputs").fetchone()[0] == 0
    with pytest.raises(CompactionJournalError, match="coverage floor"):
        journal.reserve_selected_summary(str(saved), source)
    with (
        pytest.raises(CompactionJournalError, match="prewrite marker"),
        journal.ordinary_input_send_fence(saved),
    ):
        pass
    journal.reserve_private_raw_input(saved, "a" * 32)
    with journal.ordinary_input_send_fence(saved, private_input_id="a" * 32):
        pass  # Existing ordinary private N/K raw dispatch stays available.
    with pytest.raises(CompactionJournalError, match="coverage floor"):
        journal.reserve_selected_summary(str(saved), source)
    assert journal.reserve_selected_summary(session, source)


def test_private_raw_prewrite_marker_blocks_only_its_saved_session(reserved):
    journal, session, source = reserved
    other = Path(session).with_name("other.jsonl")
    other.write_text('{"type":"session","id":"other"}\n')
    journal.reserve_private_raw_input(Path(session), "a" * 32)
    reopened = CompactionJournal(journal.path)
    with pytest.raises(CompactionJournalError, match="never replay"):
        reopened.reserve_selected_summary(session, source)
    with reopened.ordinary_input_send_fence(Path(session), private_input_id="a" * 32):
        pass  # The exact prewrite marker permits its own PR94 raw input only.
    reopened.reserve_private_raw_input(Path(session), "b" * 32)
    with reopened.ordinary_input_send_fence(Path(session), private_input_id="b" * 32):
        pass  # Ordinary next private N/K turn remains available.
    with (
        pytest.raises(CompactionJournalError, match="marker required"),
        reopened.ordinary_input_send_fence(Path(session), private_input_id="c" * 32),
    ):
        pass
    assert reopened.reserve_selected_summary(str(other), source)


def test_private_raw_marker_and_selected_reservation_share_symlink_alias_identity(reserved):
    journal, session, source = reserved
    alias = Path(session).with_name("alias.jsonl")
    alias.symlink_to(Path(session))
    journal.reserve_private_raw_input(alias, "a" * 32)
    with pytest.raises(CompactionJournalError, match="never replay"):
        journal.reserve_selected_summary(session, source)
    other = Path(session).with_name("other.jsonl")
    other.write_text("{}\n")
    journal.reserve_selected_summary(str(other), source)
    with (
        pytest.raises(CompactionJournalError, match="blocks native input"),
        journal.ordinary_input_send_fence(other),
    ):
        pass
    # A selected reservation through a symlink uses the exact real session.
    fresh = Path(session).with_name("fresh.jsonl")
    fresh.write_text("{}\n")
    fresh_alias = Path(session).with_name("fresh-alias.jsonl")
    fresh_alias.symlink_to(fresh)
    journal.reserve_selected_summary(str(fresh_alias), source)
    with (
        pytest.raises(CompactionJournalError, match="blocks native input"),
        journal.ordinary_input_send_fence(fresh),
    ):
        pass


@pytest.mark.parametrize("moment", ["before-write", "after-write"])
def test_private_raw_marker_survives_child_crash_before_selected_reserve(reserved, moment):
    journal, session, source = reserved
    marker = Path(session).with_name("fake-local-raw-write")
    script = """
import os,sys
from pathlib import Path
from agent_comms.compaction_journal import CompactionJournal
j=CompactionJournal(Path(sys.argv[1]))
j.reserve_private_raw_input(Path(sys.argv[2]),'a'*32)
if sys.argv[3]=='after-write':
    fd=os.open(sys.argv[4],os.O_CREAT|os.O_EXCL|os.O_WRONLY,0o600)
    os.write(fd,b'FAKE RAW BYTES')
    os.close(fd)
os._exit(0)
"""
    process = subprocess.run(
        [sys.executable, "-c", script, str(journal.path), session, moment, str(marker)],
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert process.returncode == 0, process.stderr
    assert marker.exists() is (moment == "after-write")
    reopened = CompactionJournal(journal.path)
    with pytest.raises(CompactionJournalError, match="never replay"):
        reopened.reserve_selected_summary(session, source)


def test_private_raw_prewrite_parent_fsync_unknown_never_writes_or_retries(reserved, monkeypatch):
    journal, session, source = reserved
    fsync = os.fsync
    monkeypatch.setattr(os, "fsync", lambda _fd: (_ for _ in ()).throw(OSError("denied")))
    with pytest.raises(CompactionJournalUnknownError, match="never dispatch"):
        journal.reserve_private_raw_input(Path(session), "a" * 32)
    monkeypatch.setattr(os, "fsync", fsync)
    reopened = CompactionJournal(journal.path)
    # The marker may be visible despite UNKNOWN; raw os.write has not run.
    with pytest.raises(CompactionJournalError, match="never replay"):
        reopened.reserve_selected_summary(session, source)
    with pytest.raises(CompactionJournalError, match="already reserved"):
        reopened.reserve_private_raw_input(Path(session), "a" * 32)


def test_link_requires_exact_committed_native_intent_binding(reserved):
    journal, session, source = reserved
    wrong = journal.begin(
        session,
        {"selectedSummaryOperationId": "0" * 32},
        inputs=InputDispositions(journal.path.parent / InputDispositions.filename).read(),
    )
    journal.resolve(wrong, CommittedOperation(), {"fixture": "metadata"})
    operation_id = journal.reserve_selected_summary(session, source)
    with pytest.raises(CompactionJournalError, match="Exact committed"):
        journal.link_selected_summary_commit(operation_id, wrong)
    commit_id = journal.begin(
        session,
        {"selectedSummaryOperationId": operation_id},
        inputs=InputDispositions(journal.path.parent / InputDispositions.filename).read(),
    )
    with pytest.raises(CompactionJournalError, match="Exact committed"):
        journal.link_selected_summary_commit(operation_id, commit_id)
    journal.resolve(commit_id, CommittedOperation(), {"fixture": "metadata"})
    journal.link_selected_summary_commit(operation_id, commit_id)
    assert journal.selected_summary(operation_id).state.commit_id == commit_id
    assert journal.unresolved_selected_summary(session) == ()
    assert journal.blocking_selected_summary(session) == (journal.selected_summary(operation_id),)
    assert not native_input_admitted(journal.path.parent, session)
    with pytest.raises(CompactionJournalError, match="unrelated native commit"):
        journal.begin(
            session,
            {},
            inputs=InputDispositions(journal.path.parent / InputDispositions.filename).read(),
        )
    with pytest.raises(CompactionJournalError, match="never replay"):
        journal.reserve_selected_summary(session, source, operation_id=operation_id)
    with pytest.raises(CompactionJournalError, match="transition|Exact committed"):
        journal.mark_selected_summary_unknown(operation_id)


def test_selected_reservation_two_process_race_has_exactly_one_winner(reserved):
    journal, session, source = reserved
    script = """
import json,sys
from pathlib import Path
from agent_comms.compaction_journal import CompactionJournal,CompactionJournalError
j=CompactionJournal(Path(sys.argv[1]))
sys.stdin.buffer.read(1)
try:
    j.reserve_selected_summary(sys.argv[2],json.loads(sys.argv[3]),operation_id=sys.argv[4])
except CompactionJournalError:
    print('blocked')
else:
    print('reserved')
"""
    workers = [
        subprocess.Popen(
            [sys.executable, "-c", script, str(journal.path), session, json.dumps(source), c * 32],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        for c in "ab"
    ]
    for worker in workers:
        assert worker.stdin is not None
        worker.stdin.write("G")
        worker.stdin.close()
    outcomes = []
    for worker in workers:
        assert worker.stdout is not None and worker.stderr is not None
        worker.wait(timeout=10)
        assert worker.returncode == 0, worker.stderr.read()
        outcomes.append(worker.stdout.read().strip())
    assert sorted(outcomes) == ["blocked", "reserved"]
    assert len(journal.blocking_selected_summary(session)) == 1
    assert not native_input_admitted(journal.path.parent, session)


def test_selected_attempt_does_not_block_another_session(reserved):
    journal, session, source = reserved
    other = journal.path.parent / "other-session.jsonl"
    other.write_text("{}\n")
    journal.reserve_selected_summary(session, source)
    assert native_input_admitted(journal.path.parent, str(other))
    assert not native_input_admitted(journal.path.parent, session)


def test_mark_unknown_fsync_fault_stays_unresolved(reserved, monkeypatch):
    journal, session, source = reserved
    operation_id = journal.reserve_selected_summary(session, source)
    fsync = os.fsync
    monkeypatch.setattr(os, "fsync", lambda _fd: (_ for _ in ()).throw(OSError("denied")))
    with pytest.raises(CompactionJournalUnknownError, match="never dispatch"):
        journal.mark_selected_summary_unknown(operation_id)
    monkeypatch.setattr(os, "fsync", fsync)
    assert journal.selected_summary(operation_id).state.declared_name in {"reserved", "unknown"}
    assert not native_input_admitted(journal.path.parent, session)


def test_competing_native_begin_refused_unless_exact_reserved_operation_bound(reserved):
    journal, session, source = reserved
    operation_id = journal.reserve_selected_summary(session, source)
    for intent in ({}, {"selectedSummaryOperationId": "f" * 32}):
        with pytest.raises(CompactionJournalError, match="unrelated native commit"):
            journal.begin(
                session,
                intent,
                inputs=InputDispositions(journal.path.parent / InputDispositions.filename).read(),
            )
    assert journal.unresolved(session) == ()
    commit_id = journal.begin(
        session,
        {"selectedSummaryOperationId": operation_id},
        inputs=InputDispositions(journal.path.parent / InputDispositions.filename).read(),
    )
    assert journal.get(commit_id).state.declared_name == "intent"
    assert not native_input_admitted(journal.path.parent, session)
    journal.resolve(commit_id, AbortedNoWriteOperation(), {"status": "aborted-no-write"})
    journal.mark_selected_summary_unknown(operation_id)
    with pytest.raises(CompactionJournalError, match="not a commit reservation"):
        journal.begin(
            session,
            {"selectedSummaryOperationId": operation_id},
            inputs=InputDispositions(journal.path.parent / InputDispositions.filename).read(),
        )


@pytest.mark.parametrize("reason", ["split_turn", "unsupported"])
def test_exact_prestart_clean_decline_is_recorded_but_not_send_authority(reserved, reason):
    journal, session, source = reserved
    operation_id = journal.reserve_selected_summary(session, source)
    journal.decline_selected_summary_prestart(operation_id, reason)
    attempt = journal.selected_summary(operation_id)
    assert (
        attempt.state.declared_name == "declined-prestart"
        and attempt.state.decline_reason == reason
    )
    assert journal.blocking_selected_summary(session) == (attempt,)
    assert not native_input_admitted(journal.path.parent, session)
    with pytest.raises(CompactionJournalError, match="never replay"):
        journal.reserve_selected_summary(session, source)
    with pytest.raises(CompactionJournalError, match="prestart decline"):
        journal.decline_selected_summary_prestart(operation_id, reason)


@pytest.mark.parametrize("terminal", ["linked", "declined-prestart"])
def test_terminal_postcommit_fsync_unknown_blocks_across_reopen(reserved, monkeypatch, terminal):
    journal, session, source = reserved
    operation_id = journal.reserve_selected_summary(session, source, operation_id="a" * 32)
    if terminal == "linked":
        commit_id = journal.begin(
            session,
            {"selectedSummaryOperationId": operation_id},
            inputs=InputDispositions(journal.path.parent / InputDispositions.filename).read(),
        )
        journal.resolve(commit_id, CommittedOperation(), {"fixture": "metadata"})
    original_fsync = os.fsync

    def deny_fsync(_fd):
        raise OSError("post-COMMIT directory fsync denied")

    monkeypatch.setattr(os, "fsync", deny_fsync)
    with pytest.raises(CompactionJournalUnknownError, match="never dispatch"):
        if terminal == "linked":
            journal.link_selected_summary_commit(operation_id, commit_id)
        else:
            journal.decline_selected_summary_prestart(operation_id, "split_turn")
    # Even a second fsync failure in the final-send read must fail closed.
    assert not native_input_admitted(journal.path.parent, session)
    monkeypatch.setattr(os, "fsync", original_fsync)
    assert journal.selected_summary(operation_id).state.declared_name == terminal
    assert not native_input_admitted(journal.path.parent, session)
    reopened = CompactionJournal(journal.path)
    assert reopened.blocking_selected_summary(session) == (reopened.selected_summary(operation_id),)
    assert not native_input_admitted(journal.path.parent, session)
    fresh = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys; from pathlib import Path; "
            "from agent_comms.compaction_send_admission import native_input_admitted; "
            "print(native_input_admitted(Path(sys.argv[1]),sys.argv[2]))",
            str(journal.path.parent),
            session,
        ],
        capture_output=True,
        text=True,
        check=True,
        timeout=10,
    )
    assert fresh.stdout.strip() == "False"
    with pytest.raises(CompactionJournalError, match="never replay"):
        reopened.reserve_selected_summary(session, source)
    with pytest.raises(CompactionJournalError, match="unrelated native commit"):
        reopened.begin(
            session,
            {},
            inputs=InputDispositions(reopened.path.parent / InputDispositions.filename).read(),
        )


@pytest.mark.parametrize(
    "reason", ["busy", "source_mismatch", "model_mismatch", "settings_mismatch"]
)
def test_uncertain_or_changed_source_decline_is_not_a_clean_skip(reserved, reason):
    journal, session, source = reserved
    operation_id = journal.reserve_selected_summary(session, source)
    with pytest.raises(CompactionJournalError, match="not a clean skip"):
        journal.decline_selected_summary_prestart(operation_id, reason)
    journal.mark_selected_summary_unknown(operation_id)
    with pytest.raises(CompactionJournalError):
        journal.decline_selected_summary_prestart(operation_id, "split_turn")
    assert not native_input_admitted(journal.path.parent, session)


def test_native_unknown_refuses_summary_reservation(reserved):
    journal, session, source = reserved
    journal.begin(
        session,
        {"witness": "native"},
        inputs=InputDispositions(journal.path.parent / InputDispositions.filename).read(),
    )
    with pytest.raises(CompactionJournalError, match="Unresolved native"):
        journal.reserve_selected_summary(session, source)
    assert journal.unresolved_selected_summary(session) == ()


def test_postcommit_fsync_fault_may_leave_blocking_summary_intent(reserved, monkeypatch):
    journal, session, source = reserved
    fsync = os.fsync
    monkeypatch.setattr(os, "fsync", lambda _fd: (_ for _ in ()).throw(OSError("denied")))
    with pytest.raises(CompactionJournalUnknownError, match="never dispatch"):
        journal.reserve_selected_summary(session, source, operation_id="b" * 32)
    monkeypatch.setattr(os, "fsync", fsync)
    assert journal.selected_summary("b" * 32).state.declared_name == "reserved"
    assert not native_input_admitted(journal.path.parent, session)


def test_reservation_survives_crash_and_never_repeats_id(tmp_path):
    session = tmp_path / "session.jsonl"
    session.write_text("{}\n")
    source = {
        "source": {"witnessRevision": "r"},
        "selected": {"provider": "fixture", "modelId": "fixture", "contextWindow": 1000},
        "settings": {"reserveTokens": 100, "keepRecentTokens": 100},
    }
    path = tmp_path / "compaction-commits.sqlite3"
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            """
import os,sys,json
from pathlib import Path
from agent_comms.compaction_journal import CompactionJournal
CompactionJournal(Path(sys.argv[1])).reserve_selected_summary(
    sys.argv[2], json.loads(sys.argv[3]), operation_id='c'*32)
os._exit(17)
""",
            str(path),
            str(session),
            json.dumps(source),
        ],
        check=False,
        timeout=10,
    )
    assert result.returncode == 17
    assert CompactionJournal(path).selected_summary("c" * 32).state.declared_name == "reserved"
    assert not native_input_admitted(tmp_path, str(session))


def test_invalid_source_or_id_refuses_before_reservation(reserved):
    journal, session, source = reserved
    with pytest.raises((ValueError, TypeError)):
        journal.reserve_selected_summary(session, {})
    with pytest.raises(ValueError, match="operation ID"):
        journal.reserve_selected_summary(session, source, operation_id="not-hex")
    assert journal.unresolved_selected_summary(session) == ()
