"""Original-byte ownership survives appends and refuses altered proof/source."""

import asyncio
import json
import os
import sqlite3
import threading
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor

import pytest

from agent_comms.native_entries import NativeEntry, NativeEvidenceRead, NativeInputEvidenceRead, NativeEvidenceScope
from agent_comms.native_input_record import NativeInputLocation
from agent_comms.native_transcript import NativeTranscript
from agent_comms.native_pi import NativeContextJournal, NativeContextProof, NativePiUnavailable, PrivateEvidenceRead
from agent_comms.coordinator import Coordination
from agent_comms.store_files import StoreLockContention, _store_lock
from test_native_pi import INPUT_ID, _evidence


@pytest.mark.parametrize("reader_type", [PrivateEvidenceRead, NativeEvidenceRead, NativeInputEvidenceRead])
@pytest.mark.parametrize("redirect", ["hardlink", "symlink"])
def test_every_native_reader_refuses_linked_or_redirected_session(tmp_path, reader_type, redirect):
    session = _evidence(tmp_path)
    before = session.read_bytes()
    alias = tmp_path / "alias.jsonl"
    if redirect == "hardlink":
        os.link(session, alias)
    else:
        alias.symlink_to(session)
    with pytest.raises(NativePiUnavailable):
        with reader_type.open(alias):
            pytest.fail("An unsafe native source was acquired")
    assert session.read_bytes() == before


def test_held_native_reader_refuses_a_new_hardlink(tmp_path):
    # Every decoder borrows this physical source. Check its lifetime once,
    # rather than restating the same assertion for each decoding capability.
    session = _evidence(tmp_path)
    with PrivateEvidenceRead.open(session) as reader:
        tuple(reader.rows())
        os.link(session, tmp_path / "new-link.jsonl")
        with pytest.raises(NativePiUnavailable):
            tuple(reader.rows())






@pytest.mark.parametrize("rewrite", ["in_place", "replace"])
def test_acquired_input_source_reverifies_rewritten_file_from_start(tmp_path, rewrite):
    session = _evidence(tmp_path)
    restored_input = "d" * 32
    restored = (session.read_bytes().replace(b'"sid"', b'"new"')
                .replace(INPUT_ID.encode(), restored_input.encode())
                .replace(b"separate", b"restored"))
    _header, original = NativeTranscript(session).forward(0, session.stat().st_size)
    location = NativeInputLocation(INPUT_ID, "sid", "entry", session_entry_start=original.start)
    restored_location = NativeInputLocation(
        restored_input, "new", "entry", session_entry_start=original.start,
    )
    with NativeEvidenceScope() as scope:
        evidence = scope.for_source(session)
        header, entries = evidence.observe(location)
        assert header.id == "sid" and tuple(evidence.inputs) == (INPUT_ID,)
        if rewrite == "in_place":
            assert len(restored) == session.stat().st_size
            session.write_bytes(restored)
        else:
            replacement = session.with_suffix(".restored")
            replacement.write_bytes(restored)
            replacement.chmod(0o600)
            replacement.replace(session)
        header, entries = evidence.observe(restored_location)
        assert header.id == "new"
        assert tuple(evidence.inputs) == (restored_input,)
        assert evidence.source.size == len(restored)








@pytest.mark.parametrize("operation", ["append", "alter_prefix"])
def test_byte_snapshot_rechecks_original_prefix_after_decoding(tmp_path, operation):
    session = _evidence(tmp_path)
    with PrivateEvidenceRead.open(session) as source:
        rows = source.rows()
        assert next(rows)["type"] == "session"
        if operation == "append":
            with session.open("a") as output:
                output.write(json.dumps({"type": "compaction", "timestamp": "2026-10-01T00:00:00.000Z", "id": "new", "summary": "next snapshot"}) + "\n")
            assert len(tuple(rows)) == 1
            assert tuple(source.rows())[0]["id"] == "new"
        else:
            session.write_bytes(session.read_bytes().replace(b"separate", b"changed!"))
            with pytest.raises(NativePiUnavailable):
                tuple(rows)
            assert source.stream.closed




def test_indexed_proof_borrow_does_not_classify_consumer_sql_failure(tmp_path):
    session = _evidence(tmp_path)
    original = sqlite3.OperationalError("consumer transaction refused")
    with pytest.raises(sqlite3.OperationalError) as caught:
        with NativeContextJournal.open_evidence(session) as db:
            assert NativeContextJournal.for_input(db, INPUT_ID) is not None
            raise original
    assert caught.value is original
    with pytest.raises(sqlite3.ProgrammingError):
        db.execute("SELECT 1")


def test_consumer_throw_into_original_rows_is_not_a_file_failure(tmp_path):
    session = _evidence(tmp_path)
    original = BlockingIOError("consumer lock was unavailable")
    with PrivateEvidenceRead.open(session) as source:
        rows = source.rows()
        assert next(rows)["type"] == "session"
        with pytest.raises(BlockingIOError) as caught:
            rows.throw(original)
        assert caught.value is original
        assert source.stream.closed


def test_physical_lock_budget_closes_refused_descriptor(tmp_path):
    lock_target = tmp_path / "ownership"
    with _store_lock(lock_target):
        # Nested scopes in one execution deliberately borrow the lock. A
        # different thread must acquire its own descriptor and spend its budget.
        def compete():
            with pytest.raises(BlockingIOError):
                with _store_lock(lock_target, blocking=False, contention=StoreLockContention(.025)):
                    pytest.fail("A competing descriptor acquired exclusive ownership")
        with ThreadPoolExecutor(max_workers=1) as worker:
            worker.submit(compete).result(timeout=1)
    # The original and the refused scope both closed: immediate new acquisition
    # succeeds without any leaked earlier lock or replacement of its evidence.
    with _store_lock(lock_target, blocking=False):
        pass


def test_consumer_work_cannot_spend_physical_lock_resource(tmp_path):
    import time
    from agent_comms.store_files import StoreLockContention, _store_lock
    resource = StoreLockContention(.025)
    with _store_lock(tmp_path / "first", blocking=False, contention=resource):
        time.sleep(.04)
    with _store_lock(tmp_path / "second", blocking=False, contention=resource):
        assert resource.remaining == .025


@pytest.mark.asyncio
async def test_joined_worker_retains_original_evidence_and_owns_sqlite_until_cancelled_join(tmp_path):
    """Cancellation cannot retire borrowed source before its SQL worker closes."""
    session = _evidence(tmp_path)
    _header, original_input = NativeTranscript(session).forward(0, session.stat().st_size)
    before = session.read_bytes(), Path(str(session) + '.input-proof').read_bytes()
    path = tmp_path / 'coordination.sqlite3'
    with Coordination(str(path), clock_ms=lambda: 1234) as original:
        original.install_private_runtime()
        original.participants.register('original', 'owner', 'owner', committed=True)
        original_connection = original.session._connection
        entered, release = threading.Event(), threading.Event()
        worker_connections = []
        with NativeEntry.open_evidence(session) as evidence:
            def observe(store):
                assert store.session._connection is not original_connection
                assert store.session.now() == original.session.now()
                worker_connections.append(store.session._connection)
                with store.session.read():
                    assert store.participants.get('original').owner_thread == 'owner'
                    NativeContextProof.read_evidence(session, INPUT_ID, evidence=evidence,
                                                      session_entry_start=original_input.start)
                    entered.set()
                    assert release.wait(2), 'bounded fixture release missing'
                    assert not evidence.source.stream.closed
                    NativeContextProof.read_evidence(session, INPUT_ID, evidence=evidence,
                                                      session_entry_start=original_input.start)

            pending = asyncio.create_task(Coordination.run_async(path, observe,
                clock_ms=original.session.now))
            try:
                assert await asyncio.to_thread(entered.wait, 1)
                for _ in range(2):
                    pending.cancel()
                    await asyncio.sleep(0)
                    assert not pending.done()
                    assert not evidence.source.stream.closed
                release.set()
                with pytest.raises(asyncio.CancelledError):
                    await pending
                assert len(worker_connections) == 1
                with pytest.raises(sqlite3.ProgrammingError):
                    _ = worker_connections[0].in_transaction
                with original.session.read():
                    assert original.participants.get('original').owner_thread == 'owner'
            finally:
                release.set()
                if not pending.done():
                    pending.cancel()
                await asyncio.gather(pending, return_exceptions=True)
    assert evidence.source.stream.closed
    assert (session.read_bytes(), Path(str(session) + '.input-proof').read_bytes()) == before


