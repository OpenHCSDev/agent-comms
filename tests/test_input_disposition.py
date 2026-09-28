"""Direct input state survives a process exit without replaying an attempt."""

import json
from pathlib import Path

import pytest

from agent_comms.input_disposition import InputDispositions


def test_batch_sources_are_one_snapshot_in_requested_order(tmp_path, monkeypatch):
    store = InputDispositions(tmp_path / InputDispositions.filename)
    for seq, text in ((7, "first admitted name"), (8, "second admitted name")):
        store.record(f"bus:{seq}", seq=seq, owner="kid", admission=1, target="#team", text=text)
    before = store.path.read_bytes()
    reads = 0
    read = type(store)._read_unlocked

    def count_reads(self):
        nonlocal reads
        reads += 1
        return read(self)

    monkeypatch.setattr(type(store), "_read_unlocked", count_reads)
    assert store.read().source_texts(("bus:8", "bus:7")) == (
        "second admitted name",
        "first admitted name",
    )
    assert reads == 1
    assert store.read().source_texts(("bus:7", "bus:9")) is None
    assert store.path.read_bytes() == before  # No receipt, review, or replay authorization.


def test_batch_sources_use_ledger_validation(tmp_path):
    store = InputDispositions(tmp_path / InputDispositions.filename)
    store.record("bus:7", seq=7, owner="kid", admission=1, target="#team", text="admitted")
    data = json.loads(store.path.read_text())
    data["rows"]["bus:7"]["source_text"] = None
    store.path.write_text(json.dumps(data))
    with pytest.raises(ValueError, match="Expected"):
        store.read().source_texts(("bus:7",))


@pytest.mark.parametrize("invalid_text", [["list"], [], {"text": "dict"}, 7, False, None])
def test_record_rejects_nonstring_before_durable_unknown(tmp_path: Path, invalid_text) -> None:
    store = InputDispositions(tmp_path / InputDispositions.filename)
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
    store = InputDispositions(tmp_path / InputDispositions.filename)
    assert store.record("bus:7", seq=7, owner="kid", admission=3, target="kid", text="ask")
    assert not store.record("bus:7", seq=7, owner="kid", admission=3, target="kid", text="ask")
    assert (
        InputDispositions(tmp_path / InputDispositions.filename)
        .read()
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
        InputDispositions(tmp_path / InputDispositions.filename).read().unknown(frozenset({"kid"}))
        == ()
    )


def test_unresolved_projection_follows_rename_without_private_receipts(tmp_path: Path) -> None:
    import os

    from agent_comms.child_process import ProcessIdentity
    from agent_comms.comms import wire
    from agent_comms.threads import Thread

    comms = wire(tmp_path)
    comms.threads.register(
        Thread(
            name="kid",
            tags=frozenset(),
            worktree=str(tmp_path),
            process_identity=ProcessIdentity.capture(os.getpid()),
        )
    )
    store = InputDispositions(comms.root / InputDispositions.filename)
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
