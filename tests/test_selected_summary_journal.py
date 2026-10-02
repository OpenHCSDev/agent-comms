"""Provider-free durable pre-send selected-summary operation IDs and input gate."""

import os
import sqlite3
import subprocess
import sys
import threading
from pathlib import Path

import pytest

from agent_comms.compaction_errors import CompactionJournalError, CompactionJournalUnknownError
from agent_comms.compaction_journal import CompactionJournal
from agent_comms.compaction_records import SelectedSummaryAttempt
from agent_comms.compaction_identity import SelectedCommitReference
from agent_comms.text_digest import TextDigest
from agent_comms.compaction_send_admission import native_input_admitted
from agent_comms.compaction_states import (
    AbortedNoWriteNativeOutcome,
    CommittedNativeOutcome,
    DeclinedPrestartSummary,
    ReservedSummary,
)
from agent_comms.input_disposition import InputDispositions
from agent_comms.pi_summary_payloads import SelectedModel
from selected_summary_cases import manual_summary_record, native_intent

pytestmark = pytest.mark.skipif(os.name != "posix", reason="POSIX durable journal")


def test_raw_marker_durable_checkpoint_retains_original_sqlite_custody(tmp_path):
    from agent_comms.compaction_records import PrivateRawInput

    session = tmp_path / 'original.jsonl'
    session.write_text('{"type":"session","version":3}\n')
    journal = CompactionJournal(tmp_path / 'compaction-commits.sqlite3')
    with journal.private_inputs.admission(session, blocking=False) as admitted:
        assert PrivateRawInput.one(admitted.db, input_id='a' * 32) is None
        admitted.mark_unknown('a' * 32)
        with sqlite3.connect(journal.path, timeout=0) as concurrent:
            with pytest.raises(sqlite3.OperationalError) as refused:
                concurrent.execute(f'SELECT * FROM {PrivateRawInput.declared_name}').fetchall()
            assert refused.value.sqlite_errorcode == sqlite3.SQLITE_BUSY
        admitted.require_marker('a' * 32)
    with journal.transaction() as db:
        assert PrivateRawInput.one(db, input_id='a' * 32).input_id == 'a' * 32


@pytest.mark.parametrize('resource', ['inputs', 'journal'])
def test_raw_admission_busy_resources_leave_no_unknown_marker(tmp_path, resource):
    from contextlib import ExitStack
    from agent_comms.compaction_records import PrivateRawInput

    session = tmp_path / 'original.jsonl'
    session.write_text('{"type":"session","version":3}\n')
    journal = CompactionJournal(tmp_path / 'compaction-commits.sqlite3')
    with ExitStack() as held:
        if resource == 'inputs':
            held.enter_context(InputDispositions(tmp_path / InputDispositions.filename).locked())
            expected = BlockingIOError
        else:
            held.enter_context(journal.transaction())
            expected = sqlite3.OperationalError
        with pytest.raises(expected):
            with journal.private_inputs.admission(session, blocking=False):
                raise AssertionError('Busy original resource cannot grant raw custody')
    with journal.transaction() as db:
        assert PrivateRawInput.select(db) == []


@pytest.fixture
def reserved(tmp_path):
    session = tmp_path / "session.jsonl"
    session.write_text('{"type":"session","version":3}\n')
    journal = CompactionJournal(tmp_path / "compaction-commits.sqlite3")
    source = manual_summary_record(session)
    return journal, str(session), source


def test_reservation_is_durable_unresolved_and_blocks_every_input(reserved):
    journal, session, source = reserved
    operation_id = journal.summaries.reserve(session, source, operation_id="a" * 32)
    assert operation_id == "a" * 32
    reopened = CompactionJournal(journal.path)
    attempt = reopened.summaries.get(operation_id)
    assert attempt.state == ReservedSummary()
    assert attempt.source_json == source.journal_json()
    assert attempt.request == source
    assert reopened.summaries.unresolved(session) == (attempt,)
    assert not native_input_admitted(journal.path.parent, session)
    with pytest.raises(CompactionJournalError, match="never replay"):
        reopened.summaries.reserve(session, source, operation_id=operation_id)
    with pytest.raises(CompactionJournalError, match="never replay"):
        reopened.summaries.reserve(session, source)
    reopened.summaries.mark_unknown(operation_id)
    assert reopened.summaries.get(operation_id).state.declared_name == "unknown"
    assert not native_input_admitted(journal.path.parent, session)
    with pytest.raises(CompactionJournalError, match="Exact committed"):
        reopened.summaries.link_commit(operation_id, "b" * 32)


def test_opaque_original_proof_is_not_a_current_request_or_input_grant(reserved):
    from agent_comms.compaction_states import UnknownSummary

    journal, session, request = reserved
    # This synthetic historical proof is deliberately not current wire JSON.
    # The outside carry must authenticate real historical request transforms;
    # the runtime neither performs that carry nor derives admission from a row.
    original_bytes = "original native proof bytes: not current JSON"
    attempt = SelectedSummaryAttempt(
        "e" * 32, session, original_bytes, request, UnknownSummary()
    )
    with journal.transaction() as db:
        attempt.insert(db)
    reopened = CompactionJournal(journal.path)
    observed = reopened.summaries.get(attempt.operation_id)
    assert observed == attempt
    assert observed.request == request
    assert reopened.summaries.blocking(session) == (observed,)
    reference = SelectedCommitReference(
        attempt.operation_id, TextDigest.of(original_bytes).value
    )
    reference.require_source(observed.source_json)
    with pytest.raises(CompactionJournalError, match="source digest"):
        reference.require_source(observed.request.journal_json())
    with pytest.raises(CompactionJournalError, match="does not admit|does not grant|does not|terminal"):
        reopened.summaries.require_original_admission(observed)
    with pytest.raises(CompactionJournalError, match="never replay"):
        reopened.summaries.reserve(session, request)


def test_raw_send_fence_blocks_every_same_session_status_not_unrelated(reserved):
    journal, session, source = reserved
    other = Path(session).with_name("other.jsonl")
    other.write_text('{"type":"session","id":"other"}\n')
    with journal.private_inputs.send_fence(Path(session)):
        pass
    operation_id = journal.summaries.reserve(session, source)
    for status in ("reserved", "unknown", "declined-prestart"):
        if status == "unknown":
            journal.summaries.mark_unknown(operation_id)
        elif status == "declined-prestart":
            # An apparent terminal row is still not ordinary send authority.
            with journal.transaction() as db:
                SelectedSummaryAttempt.update(
                    db,
                    where="operation_id=?",
                    parameters=(operation_id,),
                    state=DeclinedPrestartSummary("split_turn"),
                )
        with (
            pytest.raises(CompactionJournalError, match="blocks native input"),
            journal.private_inputs.send_fence(Path(session)),
        ):
            raise AssertionError("same-session selected row must refuse prewrite")
        with journal.private_inputs.send_fence(other):
            pass


def test_raw_send_fence_serializes_concurrent_direct_reservation(reserved):
    journal, session, source = reserved
    started, finished = threading.Event(), threading.Event()
    failures: list[BaseException] = []

    def reserve() -> None:
        started.set()
        try:
            journal.summaries.reserve(session, source)
        except BaseException as error:
            failures.append(error)
        finally:
            finished.set()

    worker = threading.Thread(target=reserve, daemon=True)
    with journal.private_inputs.send_fence(Path(session)):
        worker.start()
        assert started.wait(2)
        assert not finished.wait(0.05), "reservation committed during raw-send exclusion"
    worker.join(timeout=3)
    assert finished.is_set() and not failures
    with (
        pytest.raises(CompactionJournalError, match="blocks native input"),
        journal.private_inputs.send_fence(Path(session)),
    ):
        pass


def test_unproven_private_session_cannot_reserve_selected_summary(reserved):
    journal, session, source = reserved
    private_dir = journal.path.parent / "native-sessions" / ("f" * 32)
    private_dir.mkdir(parents=True)
    saved = private_dir / "old-pr94-session.jsonl"
    saved.write_text('{"type":"session","id":"legacy"}\n')
    # This file can predate marker deployment; absence of a marker never
    # proves it has no old PR94 raw or UNKNOWN input on the same session.
    with sqlite3.connect(journal.path) as db:
        assert db.execute("SELECT count(*) FROM private_raw_inputs").fetchone()[0] == 0
    private_source = manual_summary_record(saved)
    with pytest.raises(CompactionJournalError, match="coverage floor"):
        journal.summaries.reserve(str(saved), private_source)
    with (
        pytest.raises(CompactionJournalError, match="prewrite marker"),
        journal.private_inputs.send_fence(saved),
    ):
        pass
    journal.private_inputs.reserve(saved, "a" * 32)
    with journal.private_inputs.send_fence(saved, private_input_id="a" * 32):
        pass  # Existing ordinary private N/K raw dispatch stays available.
    with pytest.raises(CompactionJournalError, match="coverage floor"):
        journal.summaries.reserve(str(saved), private_source)
    assert journal.summaries.reserve(session, source)


def test_private_raw_prewrite_marker_blocks_only_its_saved_session(reserved):
    journal, session, source = reserved
    other = Path(session).with_name("other.jsonl")
    other.write_text('{"type":"session","id":"other"}\n')
    journal.private_inputs.reserve(Path(session), "a" * 32)
    reopened = CompactionJournal(journal.path)
    with pytest.raises(CompactionJournalError, match="coverage floor"):
        reopened.summaries.reserve(session, source)
    with reopened.private_inputs.send_fence(Path(session), private_input_id="a" * 32):
        pass  # The exact prewrite marker permits its own PR94 raw input only.
    reopened.private_inputs.reserve(Path(session), "b" * 32)
    with reopened.private_inputs.send_fence(Path(session), private_input_id="b" * 32):
        pass  # Ordinary next private N/K turn remains available.
    with (
        pytest.raises(CompactionJournalError, match="marker required"),
        reopened.private_inputs.send_fence(Path(session), private_input_id="c" * 32),
    ):
        pass
    assert reopened.summaries.reserve(str(other), manual_summary_record(other))


def test_private_raw_marker_and_selected_reservation_share_symlink_alias_identity(reserved):
    journal, session, source = reserved
    alias = Path(session).with_name("alias.jsonl")
    alias.symlink_to(Path(session))
    journal.private_inputs.reserve(alias, "a" * 32)
    with pytest.raises(CompactionJournalError, match="coverage floor"):
        journal.summaries.reserve(session, source)
    other = Path(session).with_name("other.jsonl")
    other.write_text("{}\n")
    journal.summaries.reserve(str(other), manual_summary_record(other))
    with (
        pytest.raises(CompactionJournalError, match="blocks native input"),
        journal.private_inputs.send_fence(other),
    ):
        pass
    # A selected reservation through a symlink uses the exact real session.
    fresh = Path(session).with_name("fresh.jsonl")
    fresh.write_text("{}\n")
    fresh_alias = Path(session).with_name("fresh-alias.jsonl")
    fresh_alias.symlink_to(fresh)
    journal.summaries.reserve(
        str(fresh_alias), manual_summary_record(fresh_alias)
    )
    with (
        pytest.raises(CompactionJournalError, match="blocks native input"),
        journal.private_inputs.send_fence(fresh),
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
j.private_inputs.reserve(Path(sys.argv[2]),'a'*32)
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
    with pytest.raises(CompactionJournalError, match="coverage floor"):
        reopened.summaries.reserve(session, source)


def test_private_raw_prewrite_parent_fsync_unknown_never_writes_or_retries(reserved, monkeypatch):
    journal, session, source = reserved
    fsync = os.fsync
    monkeypatch.setattr(os, "fsync", lambda _fd: (_ for _ in ()).throw(OSError("denied")))
    with pytest.raises(CompactionJournalUnknownError, match="never dispatch"):
        journal.private_inputs.reserve(Path(session), "a" * 32)
    monkeypatch.setattr(os, "fsync", fsync)
    reopened = CompactionJournal(journal.path)
    # The marker may be visible despite UNKNOWN; raw os.write has not run.
    with pytest.raises(CompactionJournalError, match="coverage floor"):
        reopened.summaries.reserve(session, source)
    with pytest.raises(CompactionJournalError, match="already reserved"):
        reopened.private_inputs.reserve(Path(session), "a" * 32)


def test_link_requires_exact_committed_native_intent_binding(reserved):
    journal, session, source = reserved
    intent, owner, native_source, _ = native_intent(session)
    wrong = journal.operations.begin(
        session,
        intent, owner=owner, source=native_source,
        selected=SelectedCommitReference("0" * 32),
        inputs=InputDispositions(journal.path.parent / InputDispositions.filename).read(),
    )
    journal.operations.resolve(wrong, CommittedNativeOutcome(
        "native-entry", intent.witness.revision, intent.witness.leaf_id, intent.metadata_digest
    ))
    operation_id = journal.summaries.reserve(session, source)
    with pytest.raises(CompactionJournalError, match="Exact committed"):
        journal.summaries.link_commit(operation_id, wrong)
    commit_id = journal.operations.begin(
        session,
        intent, owner=owner, source=native_source,
        selected=SelectedCommitReference(
            operation_id, TextDigest.of(journal.summaries.get(operation_id).source_json).value
        ),
        inputs=InputDispositions(journal.path.parent / InputDispositions.filename).read(),
    )
    with pytest.raises(CompactionJournalError, match="is intent; reconcile exact ID"):
        journal.summaries.link_commit(operation_id, commit_id)
    journal.operations.resolve(commit_id, CommittedNativeOutcome(
        "native-entry", intent.witness.revision, intent.witness.leaf_id, intent.metadata_digest
    ))
    journal.summaries.link_commit(operation_id, commit_id)
    assert journal.summaries.get(operation_id).state.commit_id == commit_id
    assert journal.summaries.unresolved(session) == ()
    assert journal.summaries.blocking(session) == (journal.summaries.get(operation_id),)
    assert not native_input_admitted(journal.path.parent, session)
    with pytest.raises(CompactionJournalError, match="unrelated native commit"):
        journal.operations.begin(
            session,
            intent, owner=owner, source=native_source,
            inputs=InputDispositions(journal.path.parent / InputDispositions.filename).read(),
        )
    with pytest.raises(CompactionJournalError, match="never replay"):
        journal.summaries.reserve(session, source, operation_id=operation_id)
    with pytest.raises(CompactionJournalError, match="transition|Exact committed"):
        journal.summaries.mark_unknown(operation_id)


def test_selected_reservation_two_process_race_has_exactly_one_winner(reserved):
    journal, session, source = reserved
    script = """
import json,sys
from pathlib import Path
from agent_comms.compaction_journal import CompactionJournal,CompactionJournalError
from agent_comms.compaction_records import SelectedSummarySource
from agent_comms.field_codec import FieldCodec
j=CompactionJournal(Path(sys.argv[1]))
sys.stdin.buffer.read(1)
try:
    j.summaries.reserve(sys.argv[2],FieldCodec.decode(SelectedSummarySource, json.loads(sys.argv[3])),operation_id=sys.argv[4])
except CompactionJournalError:
    print('blocked')
else:
    print('reserved')
"""
    workers = [
        subprocess.Popen(
            [sys.executable, "-c", script, str(journal.path), session, source.journal_json(), c * 32],
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
    assert len(journal.summaries.blocking(session)) == 1
    assert not native_input_admitted(journal.path.parent, session)


def test_selected_attempt_does_not_block_another_session(reserved):
    journal, session, source = reserved
    other = journal.path.parent / "other-session.jsonl"
    other.write_text("{}\n")
    journal.summaries.reserve(session, source)
    assert native_input_admitted(journal.path.parent, str(other))
    assert not native_input_admitted(journal.path.parent, session)


def test_mark_unknown_fsync_fault_stays_unresolved(reserved, monkeypatch):
    journal, session, source = reserved
    operation_id = journal.summaries.reserve(session, source)
    fsync = os.fsync
    monkeypatch.setattr(os, "fsync", lambda _fd: (_ for _ in ()).throw(OSError("denied")))
    with pytest.raises(CompactionJournalUnknownError, match="never dispatch"):
        journal.summaries.mark_unknown(operation_id)
    monkeypatch.setattr(os, "fsync", fsync)
    assert journal.summaries.get(operation_id).state.declared_name in {"reserved", "unknown"}
    assert not native_input_admitted(journal.path.parent, session)


def test_competing_native_begin_refused_unless_exact_reserved_operation_bound(reserved):
    journal, session, source = reserved
    intent, owner, native_source, _ = native_intent(session)
    operation_id = journal.summaries.reserve(session, source)
    for selected in (None, SelectedCommitReference("f" * 32)):
        with pytest.raises(CompactionJournalError, match="unrelated native commit"):
            journal.operations.begin(
                session,
                intent, owner=owner, source=native_source, selected=selected,
                inputs=InputDispositions(journal.path.parent / InputDispositions.filename).read(),
            )
    assert journal.operations.unresolved(session) == ()
    commit_id = journal.operations.begin(
        session,
        intent, owner=owner, source=native_source,
        selected=SelectedCommitReference(
            operation_id, TextDigest.of(journal.summaries.get(operation_id).source_json).value
        ),
        inputs=InputDispositions(journal.path.parent / InputDispositions.filename).read(),
    )
    assert journal.operations.get(commit_id).state.declared_name == "intent"
    assert not native_input_admitted(journal.path.parent, session)
    journal.operations.resolve(commit_id, AbortedNoWriteNativeOutcome(
        intent.witness.revision, intent.witness.leaf_id
    ))
    journal.summaries.mark_unknown(operation_id)
    with pytest.raises(CompactionJournalError, match="not a commit reservation"):
        journal.operations.begin(
            session,
            intent, owner=owner, source=native_source,
            selected=SelectedCommitReference(
                operation_id, TextDigest.of(journal.summaries.get(operation_id).source_json).value
            ),
            inputs=InputDispositions(journal.path.parent / InputDispositions.filename).read(),
        )


@pytest.mark.parametrize("reason", ["split_turn", "unsupported"])
def test_exact_prestart_clean_decline_is_recorded_but_not_send_authority(reserved, reason):
    journal, session, source = reserved
    operation_id = journal.summaries.reserve(session, source)
    journal.summaries.decline_prestart(operation_id, reason)
    attempt = journal.summaries.get(operation_id)
    assert (
        attempt.state.declared_name == "declined-prestart"
        and attempt.state.decline_reason == reason
    )
    assert journal.summaries.blocking(session) == (attempt,)
    assert not native_input_admitted(journal.path.parent, session)
    with pytest.raises(CompactionJournalError, match="never replay"):
        journal.summaries.reserve(session, source)
    with pytest.raises(CompactionJournalError, match="prestart decline"):
        journal.summaries.decline_prestart(operation_id, reason)


@pytest.mark.parametrize("terminal", ["linked", "declined-prestart"])
def test_terminal_postcommit_fsync_unknown_blocks_across_reopen(reserved, monkeypatch, terminal):
    journal, session, source = reserved
    intent, owner, native_source, _ = native_intent(session)
    operation_id = journal.summaries.reserve(session, source, operation_id="a" * 32)
    if terminal == "linked":
        commit_id = journal.operations.begin(
            session,
            intent, owner=owner, source=native_source,
            selected=SelectedCommitReference(
                operation_id, TextDigest.of(journal.summaries.get(operation_id).source_json).value
            ),
            inputs=InputDispositions(journal.path.parent / InputDispositions.filename).read(),
        )
        journal.operations.resolve(commit_id, CommittedNativeOutcome(
            "native-entry", intent.witness.revision, intent.witness.leaf_id, intent.metadata_digest
        ))
    original_fsync = os.fsync

    def deny_fsync(_fd):
        raise OSError("post-COMMIT directory fsync denied")

    monkeypatch.setattr(os, "fsync", deny_fsync)
    with pytest.raises(CompactionJournalUnknownError, match="never dispatch"):
        if terminal == "linked":
            journal.summaries.link_commit(operation_id, commit_id)
        else:
            journal.summaries.decline_prestart(operation_id, "split_turn")
    # Even a second fsync failure in the final-send read must fail closed.
    assert not native_input_admitted(journal.path.parent, session)
    monkeypatch.setattr(os, "fsync", original_fsync)
    assert journal.summaries.get(operation_id).state.declared_name == terminal
    assert not native_input_admitted(journal.path.parent, session)
    reopened = CompactionJournal(journal.path)
    assert reopened.summaries.blocking(session) == (reopened.summaries.get(operation_id),)
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
        reopened.summaries.reserve(session, source)
    with pytest.raises(CompactionJournalError, match="unrelated native commit"):
        reopened.operations.begin(
            session,
            intent, owner=owner, source=native_source,
            inputs=InputDispositions(reopened.path.parent / InputDispositions.filename).read(),
        )


@pytest.mark.parametrize(
    "reason", ["busy", "source_mismatch", "model_mismatch", "settings_mismatch"]
)
def test_uncertain_or_changed_source_decline_is_not_a_clean_skip(reserved, reason):
    journal, session, source = reserved
    operation_id = journal.summaries.reserve(session, source)
    with pytest.raises(CompactionJournalError, match="not a clean skip"):
        journal.summaries.decline_prestart(operation_id, reason)
    journal.summaries.mark_unknown(operation_id)
    with pytest.raises(CompactionJournalError):
        journal.summaries.decline_prestart(operation_id, "split_turn")
    assert not native_input_admitted(journal.path.parent, session)


def test_native_unknown_refuses_summary_reservation(reserved):
    journal, session, source = reserved
    intent, owner, native_source, _ = native_intent(session)
    journal.operations.begin(
        session,
        intent, owner=owner, source=native_source,
        inputs=InputDispositions(journal.path.parent / InputDispositions.filename).read(),
    )
    with pytest.raises(CompactionJournalError, match="Unresolved native"):
        journal.summaries.reserve(session, source)
    assert journal.summaries.unresolved(session) == ()


def test_postcommit_fsync_fault_may_leave_blocking_summary_intent(reserved, monkeypatch):
    journal, session, source = reserved
    fsync = os.fsync
    monkeypatch.setattr(os, "fsync", lambda _fd: (_ for _ in ()).throw(OSError("denied")))
    with pytest.raises(CompactionJournalUnknownError, match="never dispatch"):
        journal.summaries.reserve(session, source, operation_id="b" * 32)
    monkeypatch.setattr(os, "fsync", fsync)
    assert journal.summaries.get("b" * 32).state.declared_name == "reserved"
    assert not native_input_admitted(journal.path.parent, session)


def test_reservation_survives_crash_and_never_repeats_id(tmp_path):
    session = tmp_path / "session.jsonl"
    session.write_text("{}\n")
    source = manual_summary_record(session, selected=SelectedModel("fixture", "fixture", 1000))
    path = tmp_path / "compaction-commits.sqlite3"
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            """
import os,sys,json
from pathlib import Path
from agent_comms.compaction_journal import CompactionJournal
from agent_comms.compaction_records import SelectedSummarySource
from agent_comms.field_codec import FieldCodec
CompactionJournal(Path(sys.argv[1])).summaries.reserve(
    sys.argv[2], FieldCodec.decode(SelectedSummarySource, json.loads(sys.argv[3])), operation_id='c'*32)
os._exit(17)
""",
            str(path),
            str(session),
            source.journal_json(),
        ],
        check=False,
        timeout=10,
    )
    assert result.returncode == 17
    assert CompactionJournal(path).summaries.get("c" * 32).state.declared_name == "reserved"
    assert not native_input_admitted(tmp_path, str(session))


def test_invalid_operation_id_refuses_before_reservation(reserved):
    journal, session, source = reserved
    with pytest.raises(ValueError, match="operation ID"):
        journal.summaries.reserve(session, source, operation_id="not-hex")
    assert journal.summaries.unresolved(session) == ()


def test_repeated_identical_refusal_preserves_blocker_without_new_authority(reserved):
    journal, session, source = reserved
    operation_id = journal.summaries.reserve(session, source)
    journal.summaries.refuse(operation_id, "source_changed")
    refused = journal.summaries.get(operation_id)
    journal.summaries.refuse(operation_id, "source_changed")
    assert journal.summaries.get(operation_id) == refused
    assert journal.summaries.blocking(session) == (refused,)
    with pytest.raises(CompactionJournalError, match="refusal transition forbidden"):
        journal.summaries.refuse(operation_id, "different observation")
    assert journal.summaries.get(operation_id) == refused
    assert not native_input_admitted(journal.path.parent, session)
