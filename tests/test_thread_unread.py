import json
import sqlite3
from contextlib import ExitStack
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

import pytest

from agent_comms.comms import wire
from agent_comms.coordination_errors import CoordinationReadUnavailable
from agent_comms.native_transcript import NativeTranscript
from agent_comms.private_path import FileRevision
from agent_comms.store_files import _store_lock
from agent_comms.threads import Thread
from agent_comms.transcripts import NativeTranscriptCursor
from agent_comms.view_unread import (
    BudgetSpent, IdleReplyPreparation, IndexSlice, ReplyIndex, ReplyIndexUpdate,
    ResumeAfter, ResumeFromStart, RevisionMoved, TranscriptReadState, TranscriptReply,
)
from native_unread_fixture import completed_counts, completed_snapshot


def append(path, role="assistant", content="Reply"):
    # Pi writes an assistant's content as a list of parts, never a bare string.
    if role == "assistant" and isinstance(content, str):
        content = [{"type": "text", "text": content}]
    with path.open("a") as stream:
        stream.write(json.dumps({"type": "message", "message": {"role": role, "content": content}}) + "\n")


def setup_thread(tmp_path):
    source = tmp_path / "session.jsonl"
    source.touch()
    comms = wire(tmp_path / "wire")
    comms.registry.declare(comms.registry.new_thread('worker', frozenset({'ci'}), str(tmp_path), session_file=str(source)))
    return comms, source


def test_reply_resume_orders_current_sources_without_an_empty_source_marker(tmp_path):
    needed = {}
    for name in ("one", "two", "three"):
        path = tmp_path / name
        path.touch()
        source = str(path)
        needed[source] = (ReplyIndex(source=source), FileRevision.capture(path))
    names = tuple(needed)
    reads = tuple(needed.values())
    assert ResumeFromStart().order(needed) == reads
    assert ResumeAfter(names[1]).order(needed) == (reads[2], reads[0], reads[1])
    assert ResumeAfter(names[-1]).order(needed) == reads
    assert ResumeAfter(str(tmp_path / "removed")).order(needed) == reads
    assert ResumeFromStart().order({}) == ()
    assert ResumeAfter(names[0]).order({}) == ()
    assert IdleReplyPreparation.future.result().resume == ResumeFromStart()


def test_prepare_results_distinguish_budget_and_revision_without_collecting_deltas(tmp_path):
    from multiprocessing import get_context
    from time import monotonic

    source = tmp_path / "session.jsonl"
    append(source)
    revision = FileRevision.capture(source)
    index = ReplyIndex(source=str(source))
    cancelled = get_context("spawn").Event()
    spent = index.prepare(revision, IndexSlice(monotonic() + 10, cancelled, records_left=0))
    assert isinstance(spent, BudgetSpent)

    replacement = tmp_path / "replacement.jsonl"
    append(replacement, content="New file")
    replacement.replace(source)
    moved = index.prepare(revision, IndexSlice(monotonic() + 10, cancelled))
    assert isinstance(moved, RevisionMoved)

    update = index.prepare(
        FileRevision.capture(source), IndexSlice(monotonic() + 10, cancelled)
    )
    assert isinstance(update, ReplyIndexUpdate)
    updates = []
    for result in (spent, moved):
        result.collect(updates)
        assert updates == []
    update.collect(updates)
    assert updates == [update]
    assert update.after.total == 1
    assert update.after.complete


def test_reply_counts_are_persistent_human_read_positions(tmp_path):
    comms, source = setup_thread(tmp_path)
    append(source, "user", "My own prompt")
    append(source, content=[{"type": "thinking", "thinking": "private"}])
    append(source, content=[{"type": "toolCall", "id": "tool", "name": "read", "arguments": {}}])
    append(source, "toolResult", "tool output")
    append(source, content=[{"type": "text", "text": "one"}, {"type": "text", "text": "two"}])
    assert completed_snapshot(comms, str(tmp_path)).thread_unread["worker"] == 1
    shown = comms.transcripts.transcript_checkpoint("worker")
    append(source, content="Arrived after the displayed checkpoint")
    comms.views.mark_thread_view_read("worker", worktree=str(tmp_path), through=shown)
    assert completed_snapshot(wire(comms.root), str(tmp_path)).thread_unread["worker"] == 1
    assert completed_snapshot(comms, str(tmp_path / "other-project")).thread_unread["worker"] == 1
    comms.messaging.send("worker", "#ci", "Channel reply").message.message_id
    comms.views.mark_channel_view_read("#ci", worktree=str(tmp_path))
    messages = comms.bus.inbox("worker")
    comms.messaging.acknowledge("worker", tuple(message.reference for message in messages))
    assert completed_snapshot(comms, str(tmp_path)).thread_unread["worker"] == 1


def test_idle_observation_does_not_reparse_transcripts_and_append_is_incremental(tmp_path):
    comms, source = setup_thread(tmp_path)
    for _ in range(50):
        append(source)
    assert completed_snapshot(comms, str(tmp_path)).thread_unread["worker"] == 50
    state = comms.views.transcript_reads
    assert comms.views.viewer_snapshot(str(tmp_path)).thread_unread["worker"] == 50
    assert state.preparation is state._idle
    through = source.stat().st_size
    append(source)
    assert comms.views.viewer_snapshot(str(tmp_path)).unread_pending == {"worker"}
    batch = state.preparation.future.result(timeout=10)
    assert len(batch.updates) == 1
    assert batch.updates[0].before.through == through
    assert len(batch.updates[0].replies) == 1
    assert completed_snapshot(comms, str(tmp_path)).thread_unread["worker"] == 51


def test_new_process_uses_reply_index_without_reparsing_history(tmp_path):
    comms, source = setup_thread(tmp_path)
    for _ in range(100):
        append(source)
    assert completed_snapshot(comms, str(tmp_path)).thread_unread["worker"] == 100
    fresh = TranscriptReadState(comms.views.transcript_reads.path)
    sources = {"worker": (str(source),)}
    try:
        # Another process preparing an update must not prevent a fresh reader
        # from opening the already committed index.
        with ExitStack() as blocked:
            database = sqlite3.connect(fresh.index_path)
            blocked.callback(database.close)
            blocked.callback(database.rollback)
            database.execute("BEGIN IMMEDIATE")
            assert fresh.counts(str(tmp_path), sources, document=fresh.reads.read()).counts["worker"] == 100
        assert fresh.preparation is fresh._idle
        through = source.stat().st_size
        append(source)
        assert fresh.counts(str(tmp_path), sources, document=fresh.reads.read()).pending == {"worker"}
        update = fresh.preparation.future.result(timeout=10).updates[0]
        assert update.before.through == through
        assert len(update.replies) == 1
        assert completed_counts(fresh, str(tmp_path), sources).counts["worker"] == 101
    finally:
        fresh.close()


def test_sidebar_native_counts_use_the_original_captured_read_document(tmp_path):
    from agent_comms.presentation import CoordinationSnapshot
    from agent_comms.read_ledger import ReadLedger

    comms, source = setup_thread(tmp_path)
    append(source)
    viewer = comms.messaging.user_identity(str(tmp_path)).name
    completed_counts(comms.views.transcript_reads, viewer, {"worker": (str(source),)})
    capture = CoordinationSnapshot.capture

    def painted_after_capture(*args, **kwargs):
        comms.views.transcript_reads.mark_read(
            viewer, str(source), source.stat().st_size, NativeTranscript(source).cut())
        return capture(*args, **kwargs)

    with (
        patch.object(CoordinationSnapshot, "capture", side_effect=painted_after_capture),
        patch.object(ReadLedger, "read", side_effect=AssertionError("Must not reopen captured ledger")),
    ):
        assert comms.views.viewer_snapshot(str(tmp_path)).thread_unread == {"worker": 1}
    assert comms.views.viewer_snapshot(str(tmp_path)).thread_unread == {"worker": 0}


def test_ready_native_sources_share_bulk_rows_and_one_predecessor_snapshot(tmp_path):
    state = TranscriptReadState(tmp_path / "read_ledger.json")
    sources = {}
    for number in range(20):
        source = tmp_path / f"session-{number}.jsonl"
        append(source)
        state.mark_read("viewer", str(source), source.stat().st_size, NativeTranscript(source).cut())
        append(source)
        sources[str(number)] = (str(source),)
    expected = dict.fromkeys(sources, 1)
    assert completed_counts(state, "viewer", sources).counts == expected
    database = state._database()
    reads = []
    database.set_trace_callback(reads.append)
    preceding = TranscriptReply.preceding

    def captured_predecessors(connection, ends):
        assert connection.in_transaction
        return preceding(connection, ends)

    try:
        with patch.object(TranscriptReply, "preceding", side_effect=captured_predecessors):
            assert state.counts("viewer", sources, document=state.reads.read()).counts == expected
        assert sum(f"SELECT * FROM {ReplyIndex.declared_name} " in sql for sql in reads) == 1
        assert sum("SELECT reply.* FROM json_each" in sql for sql in reads) == 1
        assert not database.in_transaction
        assert database.execute("PRAGMA busy_timeout").fetchone() == (0,)
    finally:
        database.set_trace_callback(None)
        state.close()


def test_native_preparation_releases_sqlite_and_losing_commit_keeps_original_ordinals(tmp_path):
    source = tmp_path / "source.jsonl"
    append(source)
    path = tmp_path / "read_ledger.json"
    first, other = TranscriptReadState(path), TranscriptReadState(path)
    sources = {"worker": (str(source),)}
    try:
        assert completed_counts(first, "viewer", sources).counts == {"worker": 1}
        assert other.counts("viewer", sources, document=other.reads.read()).counts == {"worker": 1}
        append(source)
        assert first.counts("viewer", sources, document=first.reads.read()).pending == {"worker"}
        assert other.counts("viewer", sources, document=other.reads.read()).pending == {"worker"}
        assert not first._connection.in_transaction and not other._connection.in_transaction
        for state in (first, other):
            update = state.preparation.future.result(timeout=10).updates[0]
            assert (update.before.total, update.after.total) == (1, 2)
        assert first.counts("viewer", sources, document=first.reads.read()).counts == {"worker": 2}
        assert other.counts("viewer", sources, document=other.reads.read()).counts == {"worker": 2}
        assert len(TranscriptReply.select(first._database(), source=str(source))) == 2
    finally:
        first.close()
        other.close()


def test_current_index_does_not_contend_with_an_open_previous_ui(tmp_path):
    comms, source = setup_thread(tmp_path)
    append(source)
    previous_path = comms.root / "transcript_reply_index.sqlite3"
    previous = sqlite3.connect(previous_path)
    try:
        previous.execute("CREATE TABLE still_open (value INTEGER)")
        previous.execute("INSERT INTO still_open VALUES (17)")
        assert previous.in_transaction
        assert completed_snapshot(comms, str(tmp_path)).thread_unread["worker"] == 1
        assert comms.views.transcript_reads.index_path != previous_path
        assert previous.execute("SELECT value FROM still_open").fetchone() == (17,)
        assert source.exists()
    finally:
        previous.close()


def test_many_threads_do_not_reparse_saved_histories_on_attach(tmp_path):
    comms, _ = setup_thread(tmp_path)
    sources = {}
    for number in range(80):
        source = tmp_path / f"session-{number}.jsonl"
        for _ in range(20):
            append(source)
        sources[f"thread-{number}"] = (str(source),)
    first = TranscriptReadState(comms.views.transcript_reads.path)
    assert set(completed_counts(first, "viewer", sources).counts.values()) == {20}
    first.close()
    fresh = TranscriptReadState(comms.views.transcript_reads.path)
    try:
        assert set(fresh.counts("viewer", sources, document=fresh.reads.read()).counts.values()) == {20}
        assert fresh.preparation is fresh._idle
    finally:
        fresh.close()


def test_reply_index_rebuilds_after_source_replacement(tmp_path):
    comms, source = setup_thread(tmp_path)
    for _ in range(30):
        append(source)
    assert completed_snapshot(comms, str(tmp_path)).thread_unread["worker"] == 30
    replacement = tmp_path / "new-session.jsonl"
    append(replacement)
    replacement.replace(source)
    fresh = TranscriptReadState(comms.views.transcript_reads.path)
    try:
        assert completed_counts(fresh, str(tmp_path), {"worker": (str(source),)}).counts == {"worker": 1}
    finally:
        fresh.close()


def test_corrupt_derived_index_rebuilds_from_transcript(tmp_path):
    comms, source = setup_thread(tmp_path)
    append(source)
    assert completed_snapshot(comms, str(tmp_path)).thread_unread["worker"] == 1
    index = comms.views.transcript_reads.index_path
    comms.views.transcript_reads.close()
    index.write_bytes(b"damaged cache")
    fresh = TranscriptReadState(comms.views.transcript_reads.path)
    try:
        assert completed_counts(fresh, str(tmp_path), {"worker": (str(source),)}).counts == {"worker": 1}
    finally:
        fresh.close()


def test_open_views_share_one_incremental_index_without_sharing_markers(tmp_path):
    comms, source = setup_thread(tmp_path)
    for _ in range(30):
        append(source)
    assert completed_snapshot(comms, str(tmp_path)).thread_unread["worker"] == 30
    second = wire(comms.root)
    assert second.views.transcript_reads is comms.views.transcript_reads
    assert second.views.viewer_snapshot(str(tmp_path)).thread_unread["worker"] == 30
    assert second.views.transcript_reads.preparation is second.views.transcript_reads._idle
    append(source)
    assert completed_snapshot(second, str(tmp_path)).thread_unread["worker"] == 31
    checkpoint = second.transcripts.transcript_checkpoint("worker")
    second.views.mark_thread_view_read("worker", worktree=str(tmp_path), through=checkpoint)
    assert comms.views.viewer_snapshot(str(tmp_path)).thread_unread["worker"] == 0


def test_partial_tail_and_replaced_source_do_not_lose_replies(tmp_path):
    comms, source = setup_thread(tmp_path)
    append(source)
    comms.views.mark_thread_view_read(
        "worker", worktree=str(tmp_path), through=comms.transcripts.transcript_checkpoint("worker")
    )
    with source.open("a") as stream:
        stream.write('{"type":"message","message":{"role":"assistant","content":[{"type":"text","text":"next')
    assert completed_snapshot(comms, str(tmp_path)).thread_unread["worker"] == 0
    with source.open("a") as stream:
        stream.write('"}]}}\n')
    assert completed_snapshot(comms, str(tmp_path)).thread_unread["worker"] == 1
    replacement = tmp_path / "replacement.jsonl"
    append(replacement)
    replacement.replace(source)
    assert completed_snapshot(comms, str(tmp_path)).thread_unread["worker"] == 1


def test_rename_preserves_reads_and_fresh_fork_does_not_count_parent_history(tmp_path):
    comms, source = setup_thread(tmp_path)
    append(source)
    comms.views.mark_thread_view_read(
        "worker", worktree=str(tmp_path), through=comms.transcripts.transcript_checkpoint("worker")
    )
    comms.registry.rename("worker", "renamed")
    comms.registry.declare(comms.registry.new_thread('child', frozenset(), str(tmp_path), parent='renamed', task='New task'))
    assert completed_snapshot(comms, str(tmp_path)).thread_unread == {"renamed": 0, "child": 0}
    append(source)
    assert completed_snapshot(comms, str(tmp_path)).thread_unread == {"renamed": 1, "child": 0}


def test_old_or_invalid_checkpoint_does_not_acknowledge_new_source(tmp_path):
    comms, source = setup_thread(tmp_path)
    append(source)
    stale = comms.transcripts.transcript_checkpoint("worker")
    other = tmp_path / "other.jsonl"
    append(other)
    comms.registry.register(replace(comms.registry.require("worker"), session_file=str(other)))
    with pytest.raises(ValueError, match="Transcript changed"):
        comms.views.mark_thread_view_read("worker", worktree=str(tmp_path), through=stale)
    with pytest.raises(ValueError, match="Transcript changed"):
        comms.views.mark_thread_view_read(
            "worker", worktree=str(tmp_path),
            through=NativeTranscriptCursor(str(other), other.stat().st_size + 1),
        )
    assert completed_snapshot(comms, str(tmp_path)).thread_unread["worker"] == 1


def test_bounded_batches_resume_after_process_exit_and_preserve_read_cursor(tmp_path):
    source = tmp_path / "large.jsonl"
    for _ in range(1200):
        append(source)
    path = tmp_path / "read_ledger.json"
    first = TranscriptReadState(path)
    first.mark_read("viewer", str(source), source.stat().st_size, NativeTranscript(source).cut())
    sources = {"worker": (str(source),)}
    partial = first.counts("viewer", sources, document=first.reads.read())
    assert partial.pending == {"worker"} and "worker" not in partial.counts
    first.preparation.future.result(timeout=10)
    assert first.counts("viewer", sources, document=first.reads.read()).pending == {"worker"}  # Commit one bounded prefix.
    assert 0 < ReplyIndex.one(first._database(), source=str(source)).through < source.stat().st_size
    first.close()
    fresh = TranscriptReadState(path)
    try:
        assert completed_counts(fresh, "viewer", sources).counts == {"worker": 0}
        append(source)
        assert completed_counts(fresh, "viewer", sources).counts == {"worker": 1}
    finally:
        fresh.close()


def test_large_first_source_does_not_starve_other_sources(tmp_path):
    sources = {}
    for number in range(8):
        source = tmp_path / f"large-{number}.jsonl"
        for _ in range(600):
            append(source)
        sources[str(number)] = (str(source),)
    state = TranscriptReadState(tmp_path / "read_ledger.json")
    completed = set()
    try:
        for _ in range(40):
            observed = state.counts("viewer", sources, document=state.reads.read())
            completed.update(observed.counts)
            if not observed.pending:
                break
            state.preparation.future.result(timeout=10)
        assert completed == set(sources) and not observed.pending
        assert set(observed.counts.values()) == {600}
    finally:
        state.close()


def test_close_cancels_executor_without_scanning_rest_of_native_file(tmp_path):
    from time import monotonic

    source = tmp_path / "large.jsonl"
    for _ in range(5000):
        append(source, content="real native record " * 100)
    state = TranscriptReadState(tmp_path / "read_ledger.json")
    started = monotonic()
    assert state.counts("viewer", {"worker": (str(source),)}, document=state.reads.read()).pending == {"worker"}
    pool = state._pool
    processes = tuple(pool._processes.values())
    state.close()
    assert monotonic() - started < 2
    assert state.preparation is state._idle
    assert not any(process.is_alive() for process in processes)
    with pytest.raises(RuntimeError, match="Transcript reader is closed"):
        state.counts("viewer", {"worker": (str(source),)}, document=state.reads.read())


def test_busy_index_is_not_discarded(tmp_path):
    source = tmp_path / "source.jsonl"
    append(source)
    state = TranscriptReadState(tmp_path / "read_ledger.json")
    assert completed_counts(state, "viewer", {"worker": (str(source),)}).counts == {"worker": 1}
    inode = state.index_path.stat().st_ino
    append(source)
    blocker = sqlite3.connect(state.index_path)
    blocker.execute("BEGIN IMMEDIATE")
    try:
        busy = state.counts("viewer", {"worker": (str(source),)}, document=state.reads.read())
        assert (busy.counts, busy.pending) == ({}, frozenset({"worker"}))
        assert state.index_path.stat().st_ino == inode
    finally:
        blocker.rollback()
        blocker.close()
    try:
        assert completed_counts(state, "viewer", {"worker": (str(source),)}).counts == {"worker": 2}
    finally:
        state.close()


@pytest.mark.parametrize("busy_writer", ["wire", "sqlite"])
def test_pending_publication_does_not_erase_other_committed_counts(tmp_path, busy_writer):
    sources = {name: (str(tmp_path / f"{name}.jsonl"),) for name in ("ready", "growing")}
    for (source,) in sources.values():
        append(Path(source))
    state = TranscriptReadState(tmp_path / "read_ledger.json")
    try:
        assert completed_counts(state, "viewer", sources).counts == {"ready": 1, "growing": 1}
        append(Path(sources["growing"][0]))
        assert state.counts("viewer", sources, document=state.reads.read()).pending == {"growing"}
        preparation = state.preparation
        preparation.future.result(timeout=10)
        with ExitStack() as blocked:
            if busy_writer == "wire":
                blocked.enter_context(_store_lock(tmp_path / "wire"))
            else:
                database = sqlite3.connect(state.index_path)
                blocked.callback(database.close)
                blocked.callback(database.rollback)
                database.execute("BEGIN IMMEDIATE")
            result = state.counts("viewer", sources, document=state.reads.read())
            assert result.counts == {"ready": 1}
            assert result.pending == {"growing"}
            assert state.preparation is preparation
        assert state.counts("viewer", sources, document=state.reads.read()).counts == {"ready": 1, "growing": 2}
    finally:
        state.close()


def test_oversized_native_record_is_counted_without_truncation_or_input_limit(tmp_path):
    source = tmp_path / "oversized.jsonl"
    append(source, content="Actual text " * 200000)
    state = TranscriptReadState(tmp_path / "read_ledger.json")
    try:
        first = state.counts("viewer", {"worker": (str(source),)}, document=state.reads.read())
        assert first.pending == {"worker"} and not first.counts
        assert completed_counts(state, "viewer", {"worker": (str(source),)}).counts == {"worker": 1}
    finally:
        state.close()


def test_reopen_wire_does_not_reuse_cancelled_index_owner(tmp_path):
    comms, source = setup_thread(tmp_path)
    append(source)
    assert completed_snapshot(comms, str(tmp_path)).thread_unread == {"worker": 1}
    comms.views.transcript_reads.close()
    fresh = wire(comms.root)
    assert fresh.views.transcript_reads is not comms.views.transcript_reads
    assert fresh.views.viewer_snapshot(str(tmp_path)).thread_unread == {"worker": 1}


def test_two_processes_indexing_one_growing_transcript_count_each_reply_once(tmp_path):
    from threading import Thread as Worker

    source = tmp_path / "session.jsonl"
    source.touch()
    index = tmp_path / "wire" / "read_ledger.json"
    index.parent.mkdir()
    states = [TranscriptReadState(index), TranscriptReadState(index)]
    failures = []
    done = False

    def observe(state):
        while not done and not failures:
            try:
                state.counts("viewer", {"worker": (str(source),)}, document=state.reads.read())
            except CoordinationReadUnavailable:
                continue  # The observation owner retries unavailable reads, not fake counts.
            except BaseException as error:
                failures.append(error)

    workers = [Worker(target=observe, args=(state,)) for state in states]
    try:
        for worker in workers:
            worker.start()
        for _ in range(300):
            append(source)
        done = True
        for worker in workers:
            worker.join()
        assert failures == []
        assert completed_counts(states[0], "viewer", {"worker": (str(source),)}).counts == {"worker": 300}
    finally:
        done = True
        for worker in workers:
            worker.join()
        for state in states:
            state.close()


def test_deleted_source_withdraws_the_original_preparation_without_resurrection(tmp_path, *, thread_registration):
    source = tmp_path / "source.jsonl"
    append(source)
    state = TranscriptReadState(tmp_path / "read_ledger.json")
    try:
        assert state.counts("viewer", {"worker": (str(source),)}, document=state.reads.read()).pending == {"worker"}
        state.preparation.future.result(timeout=10)
        state.remove_threads((thread_registration.new_thread('worker', frozenset(), str(tmp_path), session_file=str(source)),))
        assert state.counts("viewer", {}, document=state.reads.read()).counts == {}
        assert ReplyIndex.one(state._database(), source=str(source)) is None
    finally:
        state.close()


def test_append_commit_does_not_scan_or_delete_historical_reply_rows(tmp_path):
    source = tmp_path / "source.jsonl"
    append(source)
    state = TranscriptReadState(tmp_path / "read_ledger.json")
    sources = {"worker": (str(source),)}
    try:
        assert completed_counts(state, "viewer", sources).counts == {"worker": 1}
        append(source)
        assert state.counts("viewer", sources, document=state.reads.read()).pending == {"worker"}
        state.preparation.future.result(timeout=10)
        queries = []
        state._database().set_trace_callback(queries.append)
        assert state.counts("viewer", sources, document=state.reads.read()).counts == {"worker": 2}
        assert not any(sql.startswith("DELETE") for sql in queries)
    finally:
        state.close()
