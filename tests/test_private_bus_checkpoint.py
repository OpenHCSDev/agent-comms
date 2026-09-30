"""Provider-free private-source checkpoint crash and prefix authority tests."""

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
from agent_comms.envelope_claim_transitions import ExistingFileClaim
from agent_comms.errors import RelationViolationError
from agent_comms.messages import Message, MessageType
from agent_comms.private_bus_checkpoint import (
    certified_delivery_page_unlocked,
    install_private_bus_checkpoint,
)
from agent_comms.store_files import _store_lock
from agent_comms.threads import Thread


def _root(tmp_path: Path) -> tuple[Comms, str]:
    root = tmp_path / "wire"
    root.mkdir(mode=0o700)
    comms = Comms(root)
    for name, tags, stamp in (
        ("sender", {"team"}, 17001.0),
        ("Alice", {"team"}, 17002.0),
        ("Bob", {"team"}, 17003.0),
        ("outsider", {"else"}, 17004.0),
    ):
        comms.registry.register(
            Thread(
                name,
                frozenset(tags),
                str(tmp_path),
                process_identity=ProcessIdentity.capture(os.getpid()),
                created_at=stamp,
            )
        )
    root_id = comms.messaging.initialize_private_initial_protocol()
    return comms, root_id


def _page(comms: Comms, lookup: str, after: int = 0, limit: int = 100):
    with _store_lock(comms.bus.log.path):
        return certified_delivery_page_unlocked(
            comms.bus.log,
            comms.bus.log._private_marker_unlocked(),
            lookup,
            after=after,
            limit=limit,
        )


def test_marker_bound_complete_addressed_pages(tmp_path: Path) -> None:
    comms, root_id = _root(tmp_path)
    with pytest.raises(RelationViolationError, match="already installed"):
        install_private_bus_checkpoint(comms.bus.log)
    one = comms.messaging.send_initial_cohort("sender", "#team", "@Alice selected; Bob no-wake")
    two = comms.messaging.send_initial_cohort("sender", "outsider", "not their audience")
    three = comms.messaging.send_initial_cohort("sender", "#team", "@Bob selected; Alice no-wake")
    alice = stable_thread_lookup(17002.0)
    bob = stable_thread_lookup(17003.0)
    witness, rows, more = _page(comms, alice, limit=1)
    assert (witness.root_id, witness.through_seq, [r.message.seq for r in rows], more) == (
        root_id,
        three.seq,
        [one.seq],
        True,
    )
    assert [r.message.seq for r in _page(comms, alice, after=one.seq)[1]] == [three.seq]
    assert [r.message.seq for r in _page(comms, bob)[1]] == [one.seq, three.seq]
    assert _page(comms, stable_thread_lookup(17004.0))[1][0].message.seq == two.seq
    assert witness.offset == comms.bus.log.path.stat().st_size
    assert witness.latest_source_seq == three.seq
    assert witness.offset > 0
    reopened = Comms(comms.root)
    assert [item.message.seq for item in _page(reopened, alice)[1]] == [one.seq, three.seq]
    # Source completeness says nothing about native injection or SQL receipts.
    assert len(rows[0].decisions) == 2


def test_crash_after_bus_fsync_before_checkpoint_cold_recovers(tmp_path: Path, monkeypatch) -> None:
    import agent_comms.private_bus_checkpoint as checkpoint

    comms, _ = _root(tmp_path)
    first = comms.messaging.send_initial_cohort("sender", "Alice", "first")
    saved = _page(comms, stable_thread_lookup(17002.0))[0]
    with monkeypatch.context() as patch:
        patch.setattr(
            checkpoint,
            "append_private_bus_checkpoint_unlocked",
            lambda *_: (_ for _ in ()).throw(OSError("crash after bus fsync")),
        )
        with pytest.raises(RelationViolationError, match="outcome UNKNOWN"):
            comms.messaging.send_initial_cohort("sender", "Alice", "second")
    bus = comms.bus.log.path
    assert bus.stat().st_size > saved.offset
    witness, rows, more = _page(comms, stable_thread_lookup(17002.0))
    assert [row.message.seq for row in rows] == [first.seq, first.seq + 1]
    assert witness.offset == bus.stat().st_size and not more
    assert witness.digest != saved.digest


def test_claim_and_keyed_response_append_share_certificate(tmp_path: Path) -> None:
    import agent_comms.private_bus_checkpoint as checkpoint
    from agent_comms.coordination_response import prepare_fenced_response, publish_fenced_response
    from test_coordination_response import _ready

    case = _ready(tmp_path, direct=True)
    try:
        comms = case.comms
        resource = tmp_path / "owned.py"
        resource.write_text("owned\n")
        claim = comms.messaging.send_message(
            "sender",
            "owner",
            "claim",
            MessageType.HANDOFF,
            claims=[ExistingFileClaim(Path("owned.py"))],
        )
        assert claim.claim_transition is not None
        intent = prepare_fenced_response(
            case.store, case.bus, case.fence, "done", owner_witness=case.witness
        ).value
        publish_fenced_response(case.store, case.bus, case.fence, owner_witness=case.witness)
        response = case.bus.log.read_keyed_response(intent)
        assert response.seq == claim.seq + 1
        with comms.bus.log.certified_read() as source:
            assert source.witness.through_seq == response.seq
        with sqlite3.connect(comms.root / "private_bus_checkpoint.sqlite3") as db:
            assert (
                db.execute("SELECT key FROM response_keys").fetchone()[0] == intent.publication_key
            )
            assert db.execute("SELECT COUNT(*) FROM delivery_sources").fetchone()[0] == 2
        page_witness, page_rows, _ = _page(comms, case.owner_lookup)
        assert page_rows[0].message.seq == case.origin_seq
        assert page_witness.latest_source_seq == response.seq
        assert page_witness.through_seq == response.seq
    finally:
        case.close()


def test_early_prefix_edit_plus_complete_lagged_suffix_denied(tmp_path: Path, monkeypatch) -> None:
    import agent_comms.private_bus_checkpoint as checkpoint

    comms, _ = _root(tmp_path)
    comms.messaging.send_initial_cohort("sender", "Alice", "prefix A" + "x" * 6000)
    with monkeypatch.context() as patch:
        patch.setattr(
            checkpoint,
            "append_private_bus_checkpoint_unlocked",
            lambda *_: (_ for _ in ()).throw(OSError("crash")),
        )
        with pytest.raises(RelationViolationError, match="outcome UNKNOWN"):
            comms.messaging.send_initial_cohort("sender", "Bob", "complete lagged suffix")
    bus = comms.bus.log.path
    raw = bus.read_bytes()
    # Semantic-canonical equal-length JSON: only early insignificant whitespace
    # changes, outside the saved 4-KiB tail and without breaking sideband digest.
    assert raw.count(b'"seq": 1') == 1
    assert raw.index(b'"seq": 1') < raw.index(b"\n") - 4096
    with bus.open("r+b") as stream:
        stream.write(raw.replace(b'"seq": 1', b'"seq":\t1', 1))
        stream.flush()
        os.fsync(stream.fileno())
    with pytest.raises(RelationViolationError, match="certified prefix changed"):
        _page(comms, stable_thread_lookup(17002.0))
    assert bus.read_bytes().endswith(b"\n")


@pytest.mark.parametrize(
    "fault",
    [
        "shrink",
        "replace",
        "partial",
        "missing",
        "schema",
        "ahead",
        "root",
        "tail",
        "corrupt",
        "replace_db",
    ],
)
def test_checkpoint_faults_deny_without_rebuild(tmp_path: Path, fault: str) -> None:
    comms, _ = _root(tmp_path)
    comms.messaging.send_initial_cohort("sender", "Alice", "durable original")
    bus = comms.bus.log.path
    db_path = bus.with_name("private_bus_checkpoint.sqlite3")
    original = db_path.read_bytes()
    if fault == "shrink":
        with bus.open("r+b") as stream:
            stream.truncate(1)
    elif fault == "replace":
        replacement = bus.with_name("replacement.jsonl")
        replacement.write_bytes(bus.read_bytes())
        replacement.chmod(0o600)
        os.replace(replacement, bus)
    elif fault == "partial":
        with bus.open("ab") as stream:
            stream.write(b'{"seq":')
            stream.flush()
            os.fsync(stream.fileno())
    elif fault == "missing":
        db_path.unlink()
    elif fault == "corrupt":
        db_path.write_bytes(b"broken sqlite database")
    elif fault == "replace_db":
        replacement = db_path.with_name("replacement.sqlite3")
        replacement.write_bytes(original)
        replacement.chmod(0o600)
        os.replace(replacement, db_path)
    else:
        with sqlite3.connect(db_path) as db:
            if fault == "schema":
                db.execute("DROP TABLE addressed")
            elif fault == "ahead":
                db.execute("UPDATE prefix_certificate SET offset=offset+10")
            elif fault == "root":
                db.execute("UPDATE prefix_certificate SET root_id=?", ("0" * 32,))
            else:
                db.execute("UPDATE prefix_certificate SET tail=?", ("0" * 64,))
    with pytest.raises(RelationViolationError):
        _page(comms, stable_thread_lookup(17002.0))
    if fault not in {"schema", "ahead", "root", "tail", "corrupt", "missing"}:
        assert db_path.read_bytes() == original


def test_unknown_public_writer_cannot_make_initial_absent_audience(tmp_path: Path) -> None:
    comms, _ = _root(tmp_path)
    comms.messaging.send_initial_cohort("sender", "Alice", "sealed")
    raw = Message("sender", "Bob", "unsealed", MessageType.INFO, seq=2).to_wire()
    with comms.bus.log.path.open("ab") as stream:
        stream.write(json.dumps(raw).encode() + b"\n")
        stream.flush()
        os.fsync(stream.fileno())
    with pytest.raises(RelationViolationError, match="Unattested public initial"):
        _page(comms, stable_thread_lookup(17002.0))


def test_deleted_sql_index_cannot_certify_absent_selected_source(tmp_path: Path) -> None:
    comms, _ = _root(tmp_path)
    source = comms.messaging.send_initial_cohort("sender", "Alice", "mandatory selected")
    lookup = stable_thread_lookup(17002.0)
    assert [item.message.seq for item in _page(comms, lookup)[1]] == [source.seq]
    with sqlite3.connect(comms.root / "private_bus_checkpoint.sqlite3") as db:
        db.execute("DELETE FROM addressed WHERE lookup=? AND seq=?", (lookup, source.seq))
    with pytest.raises(RelationViolationError, match="index seal changed"):
        _page(comms, lookup)


def test_sql_index_change_plus_complete_bus_suffix_still_denies(
    tmp_path: Path, monkeypatch
) -> None:
    import agent_comms.private_bus_checkpoint as checkpoint

    comms, _ = _root(tmp_path)
    comms.messaging.send_initial_cohort("sender", "Alice", "sealed")
    with monkeypatch.context() as patch:
        patch.setattr(
            checkpoint,
            "append_private_bus_checkpoint_unlocked",
            lambda *_: (_ for _ in ()).throw(OSError("bus only")),
        )
        with pytest.raises(RelationViolationError, match="outcome UNKNOWN"):
            comms.messaging.send_initial_cohort("sender", "Alice", "complete suffix")
    with sqlite3.connect(comms.root / "private_bus_checkpoint.sqlite3") as db:
        db.execute("DELETE FROM addressed")
    with pytest.raises(RelationViolationError, match="index seal changed"):
        _page(comms, stable_thread_lookup(17002.0))


def test_sql_index_change_during_page_is_not_exhaustiveness(tmp_path: Path, monkeypatch) -> None:
    import agent_comms.private_bus_checkpoint as checkpoint

    comms, _ = _root(tmp_path)
    comms.messaging.send_initial_cohort("sender", "Alice", "source")
    path = comms.root / "private_bus_checkpoint.sqlite3"
    real_connect = checkpoint._connect

    def mutate_before_read(candidate, *, readonly=False):
        if readonly:
            with sqlite3.connect(path) as db:
                db.execute("DELETE FROM addressed")
        return real_connect(candidate, readonly=readonly)

    monkeypatch.setattr(checkpoint, "_connect", mutate_before_read)
    with pytest.raises(RelationViolationError, match="changed during its read fence"):
        _page(comms, stable_thread_lookup(17002.0))


@pytest.mark.parametrize("phase", ["before_db_commit", "after_db_commit"])
def test_pending_marker_recovers_only_after_canonical_cold_parse(
    tmp_path: Path, monkeypatch, phase: str
) -> None:
    import agent_comms.private_bus_checkpoint as checkpoint

    comms, _ = _root(tmp_path)
    comms.messaging.send_initial_cohort("sender", "Alice", "first")
    if phase == "before_db_commit":
        monkeypatch.setattr(
            checkpoint,
            "_index_row",
            lambda *_: (_ for _ in ()).throw(OSError("before SQLite COMMIT")),
        )
    else:
        monkeypatch.setattr(
            checkpoint.FinalSeal,
            "capture",
            lambda *_: (_ for _ in ()).throw(OSError("after SQLite COMMIT")),
        )
    with pytest.raises(RelationViolationError, match="outcome UNKNOWN"):
        comms.messaging.send_initial_cohort("sender", "Alice", "second")
    monkeypatch.undo()
    witness, rows, more = _page(comms, stable_thread_lookup(17002.0))
    assert [item.message.seq for item in rows] == [1, 2]
    assert witness.through_seq == 2 and not more
    assert isinstance(comms.bus.log._private_marker_unlocked().seal, checkpoint.FinalSeal)


def test_pending_checkpoint_does_not_repair_corrupt_sql_schema(tmp_path: Path, monkeypatch) -> None:
    import agent_comms.private_bus_checkpoint as checkpoint

    comms, _ = _root(tmp_path)
    with monkeypatch.context() as patch:
        patch.setattr(
            checkpoint.FinalSeal,
            "capture",
            lambda *_: (_ for _ in ()).throw(OSError("after commit")),
        )
        with pytest.raises(RelationViolationError, match="outcome UNKNOWN"):
            comms.messaging.send_initial_cohort("sender", "Alice", "pending")
    with sqlite3.connect(comms.root / "private_bus_checkpoint.sqlite3") as db:
        db.execute("DROP TABLE addressed")
    with pytest.raises(RelationViolationError, match="schema is unavailable"):
        _page(comms, stable_thread_lookup(17002.0))


def test_over_1000_initials_and_8mib_complete_lookup(tmp_path: Path, monkeypatch) -> None:
    import agent_comms.private_bus_checkpoint as checkpoint

    comms, _ = _root(tmp_path)
    for index in range(1002):
        comms.bus.publisher.publish_initial_cohort(
            Message("sender", "#team", f"@Alice item-{index} " + "x" * 7900, MessageType.INFO)
        )
    assert comms.bus.log.path.stat().st_size > 8 * 1024 * 1024
    lookup = stable_thread_lookup(17002.0)
    warm_start = time.monotonic()
    witness, page, more = _page(comms, lookup, after=900, limit=100)
    warm_ms = (time.monotonic() - warm_start) * 1000
    assert (witness.through_seq, len(page), page[0].message.seq, more) == (1002, 100, 901, True)
    assert [row.message.seq for row in _page(comms, lookup, after=1000)[1]] == [1001, 1002]
    assert _page(comms, stable_thread_lookup(17004.0))[1] == ()
    # Existing capped readers still fail closed; only the explicit certified
    # source API supplies a complete-prefix+bounded-page alternative.
    with (
        pytest.raises(RelationViolationError),
        _store_lock(comms.bus.log.path, max_bus_bytes=8 * 1024 * 1024),
    ):
        pass
    with monkeypatch.context() as patch:
        patch.setattr(
            checkpoint,
            "append_private_bus_checkpoint_unlocked",
            lambda *_: (_ for _ in ()).throw(OSError("crash")),
        )
        with pytest.raises(RelationViolationError, match="outcome UNKNOWN"):
            comms.bus.publisher.publish_initial_cohort(
                Message("sender", "Alice", "lagged-row", MessageType.INFO)
            )
    cold_start = time.monotonic()
    recovered, page, more = _page(comms, lookup, after=1000)
    cold_ms = (time.monotonic() - cold_start) * 1000
    assert (recovered.through_seq, [row.message.seq for row in page], more) == (
        1003,
        [1001, 1002, 1003],
        False,
    )
    print(f"checkpoint scale bytes={recovered.offset} warm_ms={warm_ms:.1f} cold_ms={cold_ms:.1f}")


def test_reopened_root_preserves_certified_source_and_supports_append(
    tmp_path: Path,
) -> None:
    comms, root_id = _root(tmp_path)
    one = comms.messaging.send_initial_cohort("sender", "Alice", "already committed")
    two = comms.messaging.send_initial_cohort("sender", "outsider", "other audience")
    original = comms.bus.log.path.read_bytes()
    inode = comms.bus.log.path.stat().st_ino
    reopened = Comms(comms.root)
    witness = _page(reopened, stable_thread_lookup(17002.0))[0]
    assert (witness.root_id, witness.through_seq, witness.offset) == (
        root_id,
        two.seq,
        len(original),
    )
    assert comms.bus.log.path.read_bytes() == original
    assert comms.bus.log.path.stat().st_ino == inode
    assert [r.message.seq for r in _page(reopened, stable_thread_lookup(17002.0))[1]] == [one.seq]
    three = reopened.messaging.send_initial_cohort("sender", "Alice", "after installation")
    assert three.seq == two.seq + 1
    assert [r.message.seq for r in _page(reopened, stable_thread_lookup(17002.0))[1]] == [
        one.seq,
        three.seq,
    ]


def test_warm_witness_rejects_changed_revision_even_with_complete_row(tmp_path: Path) -> None:
    comms, _ = _root(tmp_path)
    comms.messaging.send_initial_cohort("sender", "Alice", "first")
    bus = comms.bus.log.path
    original = bus.read_bytes()
    with bus.open("r+b") as stream:
        stream.write(original.replace(b'"seq": 1', b'"seq":\t1', 1))
        stream.flush()
        os.fsync(stream.fileno())
    with pytest.raises(RelationViolationError, match="prefix tail changed"):
        _page(comms, stable_thread_lookup(17002.0))
