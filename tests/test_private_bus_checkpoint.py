"""Provider-free private-source checkpoint crash and prefix authority tests."""

from __future__ import annotations

from conftest import publish_initial
import asyncio
import json
import os
import shutil
import sqlite3
import time
from contextlib import closing
from pathlib import Path

import pytest

from agent_comms.bus_publication import AgentOrigin
from agent_comms.thread_identity import RecordedThreadIncarnation
from agent_comms.child_process import ProcessIdentity
from agent_comms.comms import Comms
from agent_comms.envelope_claim_transitions import ExistingFileClaim
from agent_comms.errors import CheckpointNeedsRepairError, RelationViolationError
from agent_comms.messages import Message, InfoKind
from agent_comms.bus_source_page import AddressedPage
from agent_comms.private_bus_checkpoint import (
    install_private_bus_checkpoint,
)
from agent_comms.store_files import (
    BoundedStoreReadBudget, ExclusiveLock, SharedLock, UNBOUNDED_STORE_READ, _store_lock,
)


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
        comms.registry.declare(
            comms.registry.new_thread(name, frozenset(tags), str(tmp_path), process_identity=ProcessIdentity.capture(os.getpid()), created_at=stamp)
        )
    root_id = comms.messaging.initialize_private_initial_protocol()
    return comms, root_id


def _page(comms: Comms, lookup: str, after: int = 0, limit: int = 100):
    request = AddressedPage.capture(lookup=lookup, after_seq=after, limit=limit)
    with comms.bus.log.checked_read() as source:
        witness, captured, more = source.addressed_page(comms.bus.log, request)
    return witness, tuple(captured), more


def test_bus_read_authority_comes_from_its_declared_lock_type(tmp_path):
    comms, root_id = _root(tmp_path)
    publish_initial(comms, 'sender', '#team', 'typed bus source')
    with _store_lock(comms.bus.log.path) as generic:
        assert not hasattr(generic, "checked_read")
        assert not hasattr(generic, "source")
    with comms.bus.log.locked() as bus:
        assert bus.checked_read.marker.root_id == root_id


def test_human_duplicate_check_includes_retained_rows_without_delivery_pointers(tmp_path):
    import json
    from agent_comms.bus_publication import HumanOrigin
    from agent_comms.messages import Message, InfoKind
    from agent_comms.private_bus_checkpoint import DeliverySources
    from agent_comms.wire_metadata import WireMetadata

    comms, root_id = _root(tmp_path)
    user = comms.messaging.user_identity(str(tmp_path))
    original = Message(user.name, 'Alice', 'Retained original body', InfoKind,
                       timestamp=1.0, seq=1, sender_role=user.role)
    # An authored offline retained cut, installed by the existing prefix record
    # owner. PublicWireRecord deliberately publishes no DeliverySources row.
    with _store_lock(comms.root / 'wire'), comms.bus.log.locked():
        comms.bus.log.path.write_text(json.dumps(original.to_wire()) + '\n')
        comms.bus.log.write_metadata_unlocked(WireMetadata.private_root(
            root_id, claims=True, last_seq=1, claimed_after_seq=1))
        # Replace only this fixture's derived empty prefix record, as for the
        # original offline retained cutover. No retained bus is discarded.
        (comms.root / 'private_bus_checkpoint.sqlite3').unlink()
        install_private_bus_checkpoint(comms.bus.log, _bus_locked=True)
    with comms.bus.log.checked_read() as source:
        assert source.connection.execute(
            f'SELECT COUNT(*) FROM "{DeliverySources.declared_name}"').fetchone()[0] == 0
    with pytest.raises(RelationViolationError):
        comms.bus.publisher.publish_ordinary(original,
            origin=HumanOrigin(user.name, user.created_at, user.worktree))
    fresh = comms.messaging.send_user_message('Alice', 'New private input', worktree=str(tmp_path))
    assert fresh.seq == 2


def test_sparse_imported_prefix_keeps_references_and_accepts_fresh_human_send(tmp_path):
    from agent_comms.message_reference import MessageReference
    from agent_comms.private_bus_checkpoint import PublicMessages
    from agent_comms.wire_record import PublicWireRecord

    source, _ = _root(tmp_path)
    first = Message('sender', 'Alice', 'selected original', InfoKind, seq=7)
    second = Message('Alice', 'sender', 'selected answer', InfoKind, seq=43,
                     answers=(MessageReference(first.seq, first.message_id),))
    copied = Comms(tmp_path / 'selected')
    for thread in source.registry.snapshot().threads.values():
        copied.registry.declare(thread)
    copied.messaging.initialize_private_initial_protocol(history=(first, second))
    before = copied.bus.log.path.read_bytes()
    with copied.bus.log.checked_read() as checked:
        PublicMessages.require_contiguous(checked.connection)
        assert [(row.seq, row.previous_seq) for row in PublicMessages.select(
            checked.connection, order_by=('seq',))] == [(7, 6), (43, 42)]
    restored = tuple(Message.from_wire(json.loads(row)) for row in before.splitlines())
    assert restored == (first, second)
    fresh = copied.messaging.send_user_message('Alice', 'fresh human input', worktree=str(tmp_path))
    assert fresh.seq == 44
    assert copied.bus.log.path.read_bytes().startswith(before)
    # A gap after the imported boundary is still this root's uncertain allocation.
    after = PublicWireRecord(Message('sender', 'Alice', 'own gap', InfoKind, seq=46))
    public, = after.checkpoint_rows(0, 1, fresh.seq, second.seq)
    assert (public.seq, public.previous_seq) == (46, 44)
    with pytest.raises(ValueError):
        after.sequence_after(46)


def test_marker_bound_complete_addressed_pages(tmp_path: Path) -> None:
    comms, root_id = _root(tmp_path)
    with pytest.raises(RelationViolationError):
        install_private_bus_checkpoint(comms.bus.log)
    one = publish_initial(comms, "sender", "#team", "@Alice selected; Bob no-wake")
    two = publish_initial(comms, "sender", "outsider", "not their audience")
    three = publish_initial(comms, "sender", "#team", "@Bob selected; Alice no-wake")
    alice = RecordedThreadIncarnation("fixture", 17002.0).lookup
    bob = RecordedThreadIncarnation("fixture", 17003.0).lookup
    witness, rows, more = _page(comms, alice, limit=1)
    assert (witness.root_id, witness.through_seq, [r.message.seq for r in rows], more) == (
        root_id,
        three.seq,
        [one.seq],
        True,
    )
    assert [r.message.seq for r in _page(comms, alice, after=one.seq)[1]] == [three.seq]
    assert [r.message.seq for r in _page(comms, bob)[1]] == [one.seq, three.seq]
    assert _page(comms, RecordedThreadIncarnation("fixture", 17004.0).lookup)[1][0].message.seq == two.seq
    assert witness.offset == comms.bus.log.path.stat().st_size
    assert witness.latest_source_seq == three.seq
    assert witness.offset > 0
    reopened = Comms(comms.root)
    assert [item.message.seq for item in _page(reopened, alice)[1]] == [one.seq, three.seq]
    # Source completeness says nothing about native injection or SQL evidences.
    assert len(rows[0].decisions) == 2


def test_crash_after_bus_fsync_before_checkpoint_cold_recovers(tmp_path: Path, monkeypatch) -> None:
    import agent_comms.private_bus_checkpoint as checkpoint

    comms, _ = _root(tmp_path)
    first = publish_initial(comms, "sender", "Alice", "first")
    saved = _page(comms, RecordedThreadIncarnation("fixture", 17002.0).lookup)[0]
    with monkeypatch.context() as patch:
        patch.setattr(
            checkpoint,
            "append_private_bus_checkpoint_unlocked",
            lambda *_: (_ for _ in ()).throw(OSError("crash after bus fsync")),
        )
        with pytest.raises(RelationViolationError):
            publish_initial(comms, "sender", "Alice", "second")
    bus = comms.bus.log.path
    assert bus.stat().st_size > saved.offset
    witness, rows, more = _page(comms, RecordedThreadIncarnation("fixture", 17002.0).lookup)
    assert [row.message.seq for row in rows] == [first.seq, first.seq + 1]
    assert witness.offset == bus.stat().st_size and not more
    assert witness.digest != saved.digest


def test_claim_and_keyed_response_append_share_checked_prefix(tmp_path: Path) -> None:
    from agent_comms.coordination_response import prepare_fenced_response, publish_fenced_response
    from agent_comms.coordination_session import NativeBoundary
    from agent_comms.envelope_claim_transitions import NativeClaim
    from agent_comms.message_reference import MessageReference
    from test_coordination_response import _ready

    case = _ready(tmp_path, direct=True)
    try:
        comms = case.comms
        resource = tmp_path / "owned.py"
        resource.write_text("owned\n")
        with NativeBoundary(comms.bus) as boundary:
            owner = boundary.registry.require("sender")
            scope = NativeClaim(
                owner_claimed_generation=boundary.registry.claim_generation(owner.name),
                operation_id="a" * 32, turn_id="claim-turn", input_id="claim-input",
            )
            acquired = comms.bus.publisher._publish_acquisition_unlocked(
                boundary.bus_lock, boundary.registry, owner,
                ExistingFileClaim(Path("owned.py")).normalized(tmp_path), scope,
                activity=boundary.activity,
            )
        (claim,) = comms.bus.log.messages_for_references(
            (MessageReference(acquired.seq, acquired.message_id),)
        )
        assert claim.claim_transition is not None
        intent = prepare_fenced_response(
            case.store, case.bus, case.fence, "done",
            exact_target=case.reply_target, owner_witness=case.witness
        ).value
        publish_fenced_response(
            case.store, case.bus, case.fence,
            exact_target=case.reply_target, owner_witness=case.witness,
        )
        response = case.bus.log.read_keyed_response(intent)
        assert response.seq == claim.seq + 1
        with comms.bus.log.checked_read() as source:
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


@pytest.mark.parametrize(
    "fault",
    [
        "shrink",
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
    publish_initial(comms, "sender", "Alice", "durable original")
    bus = comms.bus.log.path
    db_path = bus.with_name("private_bus_checkpoint.sqlite3")
    original = db_path.read_bytes()
    if fault == "shrink":
        with bus.open("r+b") as stream:
            stream.truncate(1)
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
        _page(comms, RecordedThreadIncarnation("fixture", 17002.0).lookup)
    if fault not in {"schema", "ahead", "root", "tail", "corrupt", "missing"}:
        assert db_path.read_bytes() == original


def test_unknown_public_writer_cannot_make_initial_absent_audience(tmp_path: Path) -> None:
    comms, _ = _root(tmp_path)
    publish_initial(comms, "sender", "Alice", "sealed")
    raw = Message("sender", "Bob", "unsealed", InfoKind, seq=2).to_wire()
    with comms.bus.log.path.open("ab") as stream:
        stream.write(json.dumps(raw).encode() + b"\n")
        stream.flush()
        os.fsync(stream.fileno())
    with pytest.raises(RelationViolationError):
        _page(comms, RecordedThreadIncarnation("fixture", 17002.0).lookup)


def test_deleted_sql_index_cannot_check_absent_selected_source(tmp_path: Path) -> None:
    comms, _ = _root(tmp_path)
    source = publish_initial(comms, "sender", "Alice", "mandatory selected")
    lookup = RecordedThreadIncarnation("fixture", 17002.0).lookup
    assert [item.message.seq for item in _page(comms, lookup)[1]] == [source.seq]
    with sqlite3.connect(comms.root / "private_bus_checkpoint.sqlite3") as db:
        db.execute("DELETE FROM addressed WHERE lookup=? AND seq=?", (lookup, source.seq))
    with pytest.raises(RelationViolationError):
        _page(comms, lookup)


def test_sql_index_change_plus_complete_bus_suffix_still_denies(
    tmp_path: Path, monkeypatch
) -> None:
    import agent_comms.private_bus_checkpoint as checkpoint

    comms, _ = _root(tmp_path)
    publish_initial(comms, "sender", "Alice", "sealed")
    with monkeypatch.context() as patch:
        patch.setattr(
            checkpoint,
            "append_private_bus_checkpoint_unlocked",
            lambda *_: (_ for _ in ()).throw(OSError("bus only")),
        )
        with pytest.raises(RelationViolationError):
            publish_initial(comms, "sender", "Alice", "complete suffix")
    with sqlite3.connect(comms.root / "private_bus_checkpoint.sqlite3") as db:
        db.execute("DELETE FROM addressed")
    with pytest.raises(RelationViolationError):
        _page(comms, RecordedThreadIncarnation("fixture", 17002.0).lookup)


def test_pending_checkpoint_does_not_repair_corrupt_sql_schema(tmp_path: Path, monkeypatch) -> None:
    import agent_comms.private_bus_checkpoint as checkpoint

    comms, _ = _root(tmp_path)
    with monkeypatch.context() as patch:
        patch.setattr(
            checkpoint.FinalSeal,
            "capture",
            lambda *_: (_ for _ in ()).throw(OSError("after commit")),
        )
        with pytest.raises(RelationViolationError):
            publish_initial(comms, "sender", "Alice", "pending")
    with sqlite3.connect(comms.root / "private_bus_checkpoint.sqlite3") as db:
        db.execute("DROP TABLE addressed")
    with pytest.raises(RelationViolationError):
        _page(comms, RecordedThreadIncarnation("fixture", 17002.0).lookup)


def test_over_1000_initials_and_8mib_complete_lookup(tmp_path: Path) -> None:
    comms, _ = _root(tmp_path)
    for index in range(1002):
        comms.bus.publisher.publish_initial_cohort(
            Message("sender", "#team", f"@Alice item-{index} " + "x" * 7900, InfoKind), origin=AgentOrigin()
        )
    assert comms.bus.log.path.stat().st_size > 8 * 1024 * 1024
    lookup = RecordedThreadIncarnation("fixture", 17002.0).lookup
    warm_start = time.monotonic()
    witness, page, more = _page(comms, lookup, after=900, limit=100)
    warm_ms = (time.monotonic() - warm_start) * 1000
    assert (witness.through_seq, len(page), page[0].message.seq, more) == (1002, 100, 901, True)
    assert [row.message.seq for row in _page(comms, lookup, after=1000)[1]] == [1001, 1002]
    assert _page(comms, RecordedThreadIncarnation("fixture", 17004.0).lookup)[1] == ()
    # Existing capped readers still fail closed; only the explicit checked
    # source API supplies a complete-prefix+bounded-page alternative.
    with (
        pytest.raises(RelationViolationError),
        comms.bus.log.locked(read_budget=BoundedStoreReadBudget(8 * 1024 * 1024)),
    ):
        pass
    comms.bus.publisher.publish_initial_cohort(
        Message("sender", "Alice", "appended-row", InfoKind), origin=AgentOrigin()
    )
    appended_start = time.monotonic()
    appended, page, more = _page(comms, lookup, after=1000)
    appended_ms = (time.monotonic() - appended_start) * 1000
    assert (appended.through_seq, [row.message.seq for row in page], more) == (
        1003,
        [1001, 1002, 1003],
        False,
    )
    print(f"checkpoint scale bytes={appended.offset} warm_ms={warm_ms:.1f} appended_ms={appended_ms:.1f}")


def test_reopened_root_preserves_checked_source_and_supports_append(
    tmp_path: Path,
) -> None:
    comms, root_id = _root(tmp_path)
    one = publish_initial(comms, "sender", "Alice", "already committed")
    two = publish_initial(comms, "sender", "outsider", "other audience")
    original = comms.bus.log.path.read_bytes()
    inode = comms.bus.log.path.stat().st_ino
    reopened = Comms(comms.root)
    witness = _page(reopened, RecordedThreadIncarnation("fixture", 17002.0).lookup)[0]
    assert (witness.root_id, witness.through_seq, witness.offset) == (
        root_id,
        two.seq,
        len(original),
    )
    assert comms.bus.log.path.read_bytes() == original
    assert comms.bus.log.path.stat().st_ino == inode
    assert [r.message.seq for r in _page(reopened, RecordedThreadIncarnation("fixture", 17002.0).lookup)[1]] == [one.seq]
    three = publish_initial(reopened, "sender", "Alice", "after installation")
    assert three.seq == two.seq + 1
    assert [r.message.seq for r in _page(reopened, RecordedThreadIncarnation("fixture", 17002.0).lookup)[1]] == [
        one.seq,
        three.seq,
    ]


@pytest.mark.parametrize("copy_root", [False, True])
def test_copied_root_rebuilds_its_index(tmp_path: Path, copy_root: bool) -> None:
    from agent_comms.child_process import Platform
    from agent_comms.store_files import StoreLockAcquisition, _store_lock_file

    original, root_id = _root(tmp_path)
    first = publish_initial(original, "sender", "Alice", "copied original")
    original_bytes = original.bus.log.path.read_bytes()
    original_db = (original.root / "private_bus_checkpoint.sqlite3").read_bytes()
    original_inode = original.bus.log.path.stat().st_ino
    if copy_root:
        copied = Comms(shutil.copytree(original.root, tmp_path / "copied"))
    else:
        replacement = original.root / "replacement.jsonl"
        shutil.copy2(original.bus.log.path, replacement)
        replacement.replace(original.bus.log.path)
        copied = original
    log = copied.bus.log
    assert log.path.stat().st_ino != original_inode
    platform = Platform.current()
    with _store_lock_file(log.path) as lock_file:
        platform.acquire_store_lock(lock_file.fileno(), mode=SharedLock(), blocking=True)
        with pytest.raises(CheckpointNeedsRepairError):
            with StoreLockAcquisition.holding(log.path, lock_file, platform, UNBOUNDED_STORE_READ,
                                              mode=SharedLock(), lock_type=log.lock_type):
                pass
    # The real reader upgrades its lock and rebuilds the copied SQLite index.
    request = AddressedPage.capture(lookup=RecordedThreadIncarnation("fixture", 17002.0).lookup)
    witness, messages, _ = asyncio.run(log.read_checked_async(
        lambda source: source.addressed_page(log, request)
    ))
    assert witness.root_id == root_id
    assert witness.inode == log.path.stat().st_ino
    assert [(row.message.seq, row.message.body) for row in messages] == [
        (first.seq, "copied original")
    ]
    assert log.path.read_bytes() == original_bytes
    assert _page(copied, request.lookup)[0] == witness
    second = publish_initial(copied, "sender", "Alice", "after copy")
    assert second.seq == first.seq + 1
    assert [row.message.seq for row in _page(copied, request.lookup)[1]] == [first.seq, second.seq]
    if copy_root:
        assert original.bus.log.path.read_bytes() == original_bytes
        assert (original.root / "private_bus_checkpoint.sqlite3").read_bytes() == original_db


@pytest.mark.parametrize("fault", ["tail", "prefix", "root", "shrink"])
def test_copied_root_corruption_is_not_rechecked(tmp_path: Path, fault: str) -> None:
    comms, _ = _root(tmp_path)
    for _ in range(12):
        publish_initial(comms, "sender", "Alice", "retained " + "x" * 500)
    copied = Comms(shutil.copytree(comms.root, tmp_path / "copied"))
    bus = copied.bus.log.path
    if fault == "root":
        with sqlite3.connect(copied.root / "private_bus_checkpoint.sqlite3") as db:
            db.execute("UPDATE prefix_certificate SET root_id=?", ("0" * 32,))
    else:
        with bus.open("r+b") as stream:
            if fault == "shrink":
                stream.truncate(1)
            else:
                raw = stream.read()
                position = raw.rfind(b"retained") if fault == "tail" else raw.find(b"retained")
                stream.seek(position)
                stream.write(b"changed ")
    before = (copied.root / "private_bus_checkpoint.sqlite3").read_bytes()
    marker = (copied.root / "bus_meta.json").read_bytes()
    with pytest.raises(RelationViolationError):
        asyncio.run(copied.bus.log.read_checked_async(lambda source: source.witness))
    assert (copied.root / "private_bus_checkpoint.sqlite3").read_bytes() == before
    assert (copied.root / "bus_meta.json").read_bytes() == marker


def test_copied_root_rebuilds_unsealed_index_rows(tmp_path: Path) -> None:
    comms, _ = _root(tmp_path)
    first = publish_initial(comms, "sender", "Alice", "canonical original")
    copied = Comms(shutil.copytree(comms.root, tmp_path / "copied"))
    with sqlite3.connect(copied.root / "private_bus_checkpoint.sqlite3") as db:
        db.execute("DELETE FROM addressed")
        db.execute("DELETE FROM delivery_sources")
    assert [row.message.seq for row in _page(
        copied, RecordedThreadIncarnation("fixture", 17002.0).lookup
    )[1]] == [first.seq]


def test_sealed_index_with_a_stale_schema_is_rebuilt_from_the_bus(tmp_path: Path) -> None:
    from agent_comms.checkpoint_seals import FinalSeal
    from agent_comms.private_bus_checkpoint import (
        CheckedPrefix, CheckpointSchema, DeclaredCheckpointSchema, _connect,
    )
    from agent_comms.typed_table import SQLiteTableName

    comms, _ = _root(tmp_path)
    first = publish_initial(comms, "sender", "Alice", "kept through the rebuild")
    log = comms.bus.log
    path = log.path.with_name("private_bus_checkpoint.sqlite3")
    # An older runtime's index: one declared index missing, one table it no longer declares,
    # sealed by that runtime over the file as it wrote it.
    with sqlite3.connect(path) as db:
        db.execute("DROP INDEX delivery_sources_message_id_seq_idx")
        db.execute("CREATE TABLE retired_rows (offset INTEGER)")
    with _store_lock(log.path), closing(_connect(path, readonly=True)) as db:
        marker = log.private_marker_unlocked()
        marker.seal_with(FinalSeal.capture(CheckedPrefix.read_witness(db), path))
        log.write_metadata_unlocked(marker)

    lookup = RecordedThreadIncarnation("fixture", 17002.0).lookup
    assert [row.message.seq for row in _page(comms, lookup)[1]] == [first.seq]
    with closing(_connect(path, readonly=True)) as db:
        assert isinstance(CheckpointSchema.of(db), DeclaredCheckpointSchema)
        assert "retired_rows" not in {table.name for table in SQLiteTableName.stored(db)}
    second = publish_initial(comms, "sender", "Alice", "after the rebuild")
    assert [row.message.seq for row in _page(comms, lookup)[1]] == [first.seq, second.seq]


def test_copy_preserves_unknown_sequence_reservation(tmp_path: Path, monkeypatch) -> None:
    from agent_comms.errors import HumanClaimBlockedError
    from agent_comms.wire_log import WireLog

    comms, _ = _root(tmp_path)
    publish_initial(comms, "sender", "Alice", "committed")
    write = WireLog.write_metadata_unlocked

    def reserve_then_crash(log, marker):
        write(log, marker)
        if marker.last_seq == 2:
            raise OSError("crash after durable reservation")

    with monkeypatch.context() as patch:
        patch.setattr(WireLog, "write_metadata_unlocked", reserve_then_crash)
        with pytest.raises(OSError):
            publish_initial(comms, "sender", "Alice", "uncertain")
    copied = Comms(shutil.copytree(comms.root, tmp_path / "copied"))
    original = copied.bus.log.path.read_bytes()
    with pytest.raises(HumanClaimBlockedError):
        asyncio.run(copied.bus.log.read_checked_async(
            lambda source: source.require_new_human_message("human", "new-message")
        ))
    assert copied.bus.log.private_marker_unlocked().last_seq == 2
    assert copied.bus.log.path.read_bytes() == original
    with sqlite3.connect(copied.root / "private_bus_checkpoint.sqlite3") as db:
        assert db.execute("SELECT through_seq FROM prefix_certificate").fetchone() == (1,)


def test_warm_witness_rejects_changed_revision_even_with_complete_row(tmp_path: Path) -> None:
    comms, _ = _root(tmp_path)
    publish_initial(comms, "sender", "Alice", "first")
    bus = comms.bus.log.path
    original = bus.read_bytes()
    with bus.open("r+b") as stream:
        stream.write(original.replace(b'"seq": 1', b'"seq":\t1', 1))
        stream.flush()
        os.fsync(stream.fileno())
    with pytest.raises(RelationViolationError):
        _page(comms, RecordedThreadIncarnation("fixture", 17002.0).lookup)


def test_shared_reader_hands_lagged_suffix_to_exclusive_repair(tmp_path: Path, monkeypatch) -> None:
    import asyncio

    import agent_comms.private_bus_checkpoint as checkpoint
    from agent_comms.child_process import Platform
    from agent_comms.store_files import StoreLockAcquisition, _store_lock_file

    comms, _ = _root(tmp_path)
    publish_initial(comms, "sender", "Alice", "first")
    with monkeypatch.context() as patch:
        patch.setattr(
            checkpoint,
            "append_private_bus_checkpoint_unlocked",
            lambda *_: (_ for _ in ()).throw(OSError("crash")),
        )
        with pytest.raises(RelationViolationError):
            publish_initial(comms, "sender", "Bob", "lagged row")
    log = comms.bus.log
    platform = Platform.current()
    # A shared-lock reader never writes the checkpoint; it names the repair.
    with _store_lock_file(log.path) as lock_file:
        platform.acquire_store_lock(lock_file.fileno(), mode=SharedLock(), blocking=True)
        with pytest.raises(CheckpointNeedsRepairError):
            with StoreLockAcquisition.holding(log.path, lock_file, platform, UNBOUNDED_STORE_READ,
                                              mode=SharedLock(), lock_type=log.lock_type):
                pass
    # The async reader then takes the exclusive lock, which repairs and reads.
    through = asyncio.run(log.read_checked_async(lambda source: source.witness.through_seq))
    assert through == 2
    # Once repaired, a shared read succeeds without writing.
    assert asyncio.run(log.read_checked_async(lambda source: source.witness.through_seq)) == 2


@pytest.mark.parametrize(("mode", "escalated"), [(SharedLock(), ExclusiveLock), (ExclusiveLock(), None)])
def test_only_a_shared_reader_retries_a_needed_repair_exclusively(mode, escalated) -> None:
    error = CheckpointNeedsRepairError("lagged suffix")
    if escalated is None:
        with pytest.raises(CheckpointNeedsRepairError) as raised:
            mode.after_repair_needed(error)
        assert raised.value is error
    else:
        assert type(mode.after_repair_needed(error)) is escalated
