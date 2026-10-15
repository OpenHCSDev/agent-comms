"""Direct input state survives a process exit without replaying an attempt."""

import json
import multiprocessing
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from agent_comms.input_disposition import InputDispositions
from stored_inputs import stored_inputs


@pytest.mark.parametrize("invalid_text", [["list"], [], {"text": "dict"}, 7, False, None])
def test_record_rejects_nonstring_before_durable_unknown(tmp_path: Path, invalid_text) -> None:
    store = InputDispositions(tmp_path)
    with pytest.raises(ValueError, match="Invalid ACP input identity"):
        store.record(
            "acp:" + "f" * 32,
            seq=None,
            owner="kid",
            admission=1,
            target="kid",
            text=invalid_text,
        )
    assert not store.path.exists()


def test_unknown_is_durable_and_native_start_is_a_cas(tmp_path: Path) -> None:
    store = InputDispositions(tmp_path)
    assert store.record("bus:7", seq=7, owner="kid", admission=3, target="kid", text="ask")
    assert not store.record("bus:7", seq=7, owner="kid", admission=3, target="kid", text="ask")
    assert (
        InputDispositions(tmp_path)
        .unresolved(frozenset({"kid"}))
        .unknown(frozenset({"kid"}))[0]
        .sequence
        == 7
    )

    native_id = "a" * 32
    assert store.bind("bus:7", admission=3, turn_id="turn-1", native_id=native_id, text="full ask")
    assert not store.started("bus:7", turn_id="turn-1", native_id="b" * 32, text="full ask")
    assert not store.started("bus:7", turn_id="turn-1", native_id=native_id, text="wrong")
    assert store.started("bus:7", turn_id="turn-1", native_id=native_id, text="full ask")
    assert not store.started("bus:7", turn_id="turn-1", native_id=native_id, text="full ask")
    assert (
        stored_inputs(tmp_path).unknown(frozenset({"kid"}))
        == ()
    )


def test_unresolved_projection_follows_rename_without_private_receipts(tmp_path: Path) -> None:
    import os

    from agent_comms.child_process import ProcessIdentity
    from agent_comms.comms import wire
    from agent_comms.threads import Thread

    comms = wire(tmp_path)
    comms.registry.declare(
        Thread(
            name="kid",
            tags=frozenset(),
            worktree=str(tmp_path),
            process_identity=ProcessIdentity.capture(os.getpid()),
        )
    )
    store = InputDispositions(comms.root)
    store.record("bus:7", seq=7, owner="kid", admission=1, target="#review", text="exact source")
    store.record("bus:8", seq=8, owner="peer", admission=1, target="#review", text="other owner")
    store.bind("bus:7", admission=1, turn_id="turn", native_id="a" * 32, text="private wrapper")
    comms.threads.rename_managed_thread("kid", "new-kid", owner_pid=os.getpid())
    expected = [
        {
            "inputId": "bus:7",
            "sequence": 7,
            "target": "#review",
            "text": "exact source",
            "status": "unknown",
        }
    ]
    assert comms.goals.unresolved_inputs("new-kid") == expected
    assert comms.goals.unresolved_inputs("kid") == expected
    assert store.started("bus:7", turn_id="turn", native_id="a" * 32, text="private wrapper")
    assert comms.goals.unresolved_inputs("new-kid") == []


def _cold_snapshots(root, gate, results):
    store = InputDispositions(Path(root))
    errors = []
    for _ in range(16):
        gate.wait(timeout=10)
        try:
            assert store.lookup("original").source_text == "original"
        except Exception as error:
            errors.append(f"{type(error).__name__}: {error}")
        gate.wait(timeout=10)
    results.put(errors)


def test_cold_wal_snapshots_do_not_compete_for_initialization(tmp_path):
    store = InputDispositions(tmp_path)
    store.record("original", seq=None, owner="kid", admission=1, target="kid", text="original")
    context = multiprocessing.get_context("spawn")
    gate, results = context.Barrier(2), context.Queue()
    workers = [context.Process(target=_cold_snapshots, args=(str(tmp_path), gate, results))
               for _ in range(2)]
    try:
        for worker in workers:
            worker.start()
        assert [results.get(timeout=20) for _ in workers] == [[], []]
        for worker in workers:
            worker.join(timeout=5)
            assert worker.exitcode == 0
    finally:
        for worker in workers:
            if worker.is_alive():
                worker.terminate()
                worker.join(timeout=5)
        results.close()
        results.join_thread()


def test_admitted_snapshot_retains_cut_without_blocking_writer(tmp_path):
    store = InputDispositions(tmp_path)
    store.record("original", seq=None, owner="kid", admission=1, target="kid", text="original")
    with ThreadPoolExecutor(max_workers=1) as writer:
        with store.snapshot() as connection:
            appended = writer.submit(
                store.record, "later", seq=None, owner="kid", admission=1,
                target="kid", text="later",
            )
            assert appended.result(timeout=2)
            assert set(store._document(connection, "1", ()).rows) == {"original"}
    assert set(store.unresolved(frozenset({"kid"})).rows) == {"original", "later"}
