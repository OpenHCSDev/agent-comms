"""Scoped claim-envelope durability on an initialized private root.

These tests exercise the publisher boundary, not native input authorization.
Ordinary conversation sends no longer acquire or manually release files.
"""
from __future__ import annotations

from agent_comms.bus import Bus
from bus_record_fixture import prepare_native_comms

from bus_history import read_history
import json
import os
import secrets
import signal
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from threading import Barrier

import pytest

import agent_comms.store_files as store_files
from agent_comms.child_process import ProcessIdentity
from agent_comms.comms import Comms
from agent_comms.coordination_session import NativeBoundary
from agent_comms.envelope_claim_transitions import ClaimConflict, ExistingFileClaim, NativeClaim
from agent_comms.errors import ClaimEnvelopeUnknownError, RelationViolationError, StoredHistoryUnreadable
from agent_comms.field_codec import MalformedRecord
from agent_comms.exporting import EverythingScope, FullLimit, JsonlFormat
from agent_comms.messages import ClaimNoticeKind, Message, InfoKind
from agent_comms.message_reference import MessageReference
from agent_comms.thread_status import RunningThreadStatus


def marked(tmp_path: Path) -> Comms:
    return prepare_native_comms(Comms(tmp_path / "wire"))


def _sample_line(*, complete: bool = True) -> bytes:
    row = Message("author", "reader", "one", InfoKind, seq=1).to_wire()
    return json.dumps(row).encode() + (b"\n" if complete else b"")


def _participants(tmp_path: Path) -> tuple[Comms, Path]:
    worktree = tmp_path / "project"
    worktree.mkdir()
    (worktree / "a.py").write_text("one")
    (worktree / "b.py").write_text("two")
    comms = Comms(tmp_path / "wire")
    for name in ("alice", "bob", "observer"):
        owner = comms.registry.new_thread(name, frozenset({'team'}), str(worktree))
        comms.registry.declare(
            replace(owner, process_identity=ProcessIdentity.capture(os.getpid())),
            RunningThreadStatus(),
        )
    Bus.install(Bus.path_in(comms.root))
    return comms, worktree


def acquire_message(comms: Comms, name: str, path: Path) -> Message:
    """Publish one scoped tool acquisition through the production claim writer."""
    with NativeBoundary(comms.bus) as boundary:
        owner = boundary.registry.require(name)
        scope = NativeClaim(
            owner_claimed_generation=boundary.registry.claim_generation(name),
            operation_id=secrets.token_hex(16), turn_id=f"turn-{name}", input_id=f"input-{name}",
        )
        claim = comms.bus.publisher._publish_acquisition_unlocked(
            boundary.bus_lock, boundary.registry, owner,
            ExistingFileClaim(path).normalized(Path(owner.worktree)), scope,
            activity=boundary.activity,
        )
    message, = comms.bus.log.messages_for_references((MessageReference(claim.seq, claim.message_id),))
    return message


def release_messages(comms: Comms, messages: tuple[Message, ...]) -> None:
    """Retire the actual acquisition records, not caller-authored release paths."""
    claims = tuple(message.claim_transition.claimed(resource)
                   for message in messages for resource in message.claim_transition.claims)
    with NativeBoundary(comms.bus) as boundary:
        comms.bus.publisher.release_resources(boundary, claims, "Finished test tool operation")


def test_valid_json_without_newline_not_exposed_by_any_gated_read(tmp_path):
    comms = marked(tmp_path)
    comms.bus.log.path.write_bytes(_sample_line(complete=False))
    with pytest.raises(RelationViolationError):
        read_history(comms)
    with pytest.raises(RelationViolationError):
        comms.bus.log.messages_for_references((MessageReference(1, "acknowledgement"),))
    with pytest.raises(RelationViolationError):
        comms.bus.log.latest_sequence()
    with pytest.raises(RelationViolationError):
        with comms.bus.log.full_history_snapshot() as (_, messages):
            list(messages)
    with pytest.raises(RelationViolationError):
        comms.bus.log.claim_projection()
    with pytest.raises(RelationViolationError):
        comms.relationships._recent_messages()


def test_malformed_complete_row_is_not_an_ordinary_message(tmp_path):
    comms = marked(tmp_path)
    comms.bus.log.path.write_bytes(b'{"seq":1,"seq":2}\n')
    with pytest.raises(StoredHistoryUnreadable) as malformed:
        read_history(comms)
    assert isinstance(malformed.value.__cause__, ValueError)


def test_scoped_acquisition_is_one_original_and_conflict_appends_nothing(tmp_path):
    comms, worktree = _participants(tmp_path)
    sent = acquire_message(comms, "alice", Path("a.py"))
    transition = sent.claim_transition
    assert transition.claims == (str(worktree / "a.py"),)
    assert (transition.message_id, transition.seq) == (sent.message_id, sent.seq)
    assert isinstance(transition.claim_scope, NativeClaim)
    assert transition.claim_scope.input_id == "input-alice"
    assert comms.bus.log.latest_sequence() == sent.seq
    stored = json.loads(comms.bus.log.path.read_text().splitlines()[0])
    assert stored["claim_transition"]["admission"]["input_id"] == "input-alice"
    assert read_history(comms) == [sent]
    assert comms.bus.log.claim_projection()[str(worktree / "a.py")].owner == "alice"
    with pytest.raises(ClaimConflict) as loss:
        acquire_message(comms, "bob", Path("a.py"))
    assert loss.value.existing.owner == "alice"
    assert read_history(Comms(comms.root)) == [sent]


def test_absolute_claim_outside_worktree_uses_one_global_key(tmp_path):
    comms, worktree = _participants(tmp_path)
    outside = tmp_path / "shared" / "module.py"
    outside.parent.mkdir()
    outside.write_text("shared")
    claimed = acquire_message(comms, "alice", outside)
    assert claimed.claim_transition.claims == (str(outside),)
    projection = Comms(comms.root).bus.log.claim_projection()
    assert list(projection) == [str(outside)]
    assert projection[str(outside)].owner == "alice"
    with pytest.raises(ClaimConflict):
        acquire_message(comms, "bob", outside)
    release_messages(comms, (claimed,))
    assert not comms.bus.log.claim_projection()
    assert worktree != outside.parent


def test_same_file_double_slash_alias_conflicts_without_second_message(tmp_path):
    comms, worktree = _participants(tmp_path)
    canonical = str(worktree / "a.py")
    alias = str(worktree) + "//a.py"
    assert os.path.samefile(canonical, alias) and canonical != alias
    first = acquire_message(comms, "alice", Path(canonical))
    with pytest.raises(ClaimConflict) as conflict:
        acquire_message(comms, "bob", Path(alias))
    assert conflict.value.existing.owner == "alice"
    assert read_history(comms) == [first]
    assert dict(comms.bus.log.claim_projection()) == {canonical: conflict.value.existing}


def test_same_tick_new_owner_cannot_share_live_claim_identity(tmp_path):
    comms, worktree = _participants(tmp_path)
    claimed = acquire_message(comms, "alice", Path("a.py"))
    alice = comms.registry.require("alice")
    with pytest.raises(RelationViolationError):
        comms.registry.declare(comms.registry.new_thread(
            'same-tick-peer', frozenset({'team'}), str(worktree), created_at=alice.created_at))
    assert "same-tick-peer" not in comms.registry
    assert read_history(comms) == [claimed]
    assert comms.bus.log.claim_projection()[str(worktree / "a.py")].owner == "alice"
    release_messages(comms, (claimed,))
    assert str(worktree / "a.py") not in comms.bus.log.claim_projection()


def test_saved_colliding_creation_id_blocks_claim_write_before_append(tmp_path):
    comms, _ = _participants(tmp_path)
    claimed = acquire_message(comms, "alice", Path("a.py"))
    original_bytes = comms.bus.log.path.read_bytes()
    with comms.registry.store.editing() as edit:
        threads = edit.document.threads
        threads["bob"] = replace(threads["bob"], created_at=threads["alice"].created_at)
        edit.commit()
    reopened = Comms(comms.root)
    with pytest.raises(RelationViolationError):
        acquire_message(reopened, "bob", Path("a.py"))
    assert reopened.bus.log.path.read_bytes() == original_bytes
    with reopened.bus.log.full_history_snapshot() as (_, messages):
        assert list(messages) == [claimed]
    with pytest.raises(RelationViolationError):
        read_history(comms)


def test_owner_can_release_acquisition_after_file_deletion(tmp_path):
    comms, worktree = _participants(tmp_path)
    claimed = acquire_message(comms, "alice", Path("a.py"))
    (worktree / "a.py").unlink()
    release_messages(comms, (claimed,))
    assert comms.bus.log.claim_projection().get(str(worktree / "a.py")) is None
    assert len(read_history(comms)) == 2


def test_concurrent_acquisition_exactly_one_winner(tmp_path):
    comms, _ = _participants(tmp_path)
    simultaneous = Barrier(2)

    def attempt(name):
        simultaneous.wait(timeout=3)
        try:
            acquire_message(Comms(comms.root), name, Path("a.py"))
        except ClaimConflict:
            return "lost"
        return "won"

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = tuple(pool.map(attempt, ("alice", "bob")))
    assert sorted(results) == ["lost", "won"]
    assert len(read_history(comms)) == 1


def test_large_release_reopens_and_exports_without_a_second_parser_limit(tmp_path):
    comms, worktree = _participants(tmp_path)
    directory = worktree
    for number in range(10):
        directory = directory / ("d" * 180 + str(number))
        directory.mkdir()
    acquired = []
    for number in range(10):
        resource = directory / f"{number}.py"
        resource.write_text("ordinary file")
        acquired.append(acquire_message(comms, "alice", resource))
    assert len(comms.bus.log.claim_projection()) == len(acquired)
    release_messages(comms, tuple(acquired))
    history = read_history(comms)
    committed = history[-1]
    assert len(json.dumps(committed.to_wire()["claim_transition"])) > 16_384
    assert not Comms(comms.root).bus.log.claim_projection()
    assert read_history(Comms(comms.root)) == history
    assert comms.bus.log.messages_for_references((committed.reference,)) == (committed,)
    destination = tmp_path / "safe-export.jsonl"
    acknowledgement = comms.views.export_wire(
        destination, format=JsonlFormat(), scope=EverythingScope(), limit=FullLimit())
    exported = [json.loads(line) for line in destination.read_text().splitlines()]
    assert acknowledgement.exported_messages == len(history)
    assert len(exported) == len(history) + 1 and exported[-1]["record"] == "message"
    assert exported[-1]["message"] == committed.to_wire()


def test_invalid_claim_record_cannot_be_exposed_as_plain_message(tmp_path):
    comms, _ = _participants(tmp_path)
    public = Message("alice", "bob", "attempt", InfoKind, seq=1).to_wire()
    public["claim_transition"] = {"claims": ["/tmp/a.py"]}
    comms.bus.log.path.write_bytes(json.dumps(public).encode() + b"\n")
    with pytest.raises(StoredHistoryUnreadable) as malformed:
        read_history(comms)
    assert isinstance(malformed.value.__cause__, StoredHistoryUnreadable)
    assert isinstance(malformed.value.__cause__.__cause__, MalformedRecord)
    assert "claim_transition" in str(malformed.value.__cause__.__cause__)


def _checked_claim(comms, resource):
    with comms.bus.log.checked_read() as source:
        return source.claim(resource)


def _ordinary_locked_iterator(comms):
    with comms.bus.log.full_history_snapshot() as (_, messages):
        return list(messages)


def _ordinary_snapshot(comms):
    with comms.bus.log._record_snapshot() as (_, records):
        return list(records)


def test_claim_bearing_json_valid_no_newline_never_exposed(tmp_path):
    comms, worktree = _participants(tmp_path)
    committed = acquire_message(comms, "alice", Path("a.py"))
    bus = comms.bus.log.path
    assert bus.read_bytes().endswith(b"\n")
    bus.write_bytes(bus.read_bytes()[:-1])
    denied = (
        lambda: comms.bus.log.messages_for_references((committed.reference,)),
        lambda: _ordinary_locked_iterator(comms), lambda: _ordinary_snapshot(comms),
        lambda: read_history(comms), lambda: comms.bus.log.latest_sequence(),
        lambda: comms.bus.log.claim_projection(),
        lambda: _checked_claim(comms, str(worktree / "a.py")),
    )
    for read in denied:
        # Stream readers reject the row; checkpoint readers reject its changed cut.
        with pytest.raises(RelationViolationError):
            read()


def test_reserved_metadata_sequence_is_not_a_committed_message(tmp_path, monkeypatch):
    comms, _ = _participants(tmp_path)
    bus = comms.bus.log.path
    real_open = os.open

    def fail_before_append(path, flags, *args, **kwargs):
        if str(path) == str(bus) and flags & os.O_APPEND:
            raise OSError("injected failure after metadata reservation")
        return real_open(path, flags, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(store_files.os, "open", fail_before_append)
        with pytest.raises(ClaimEnvelopeUnknownError):
            acquire_message(comms, "alice", Path("a.py"))
    assert json.loads((comms.root / "bus_meta.json").read_text())["last_seq"] == 1
    assert read_history(comms) == []
    assert comms.bus.log.latest_sequence() == 0
    with comms.bus.log.full_history_snapshot() as (through, rows):
        assert through == 0 and list(rows) == []
    committed = acquire_message(comms, "bob", Path("a.py"))
    assert committed.seq == 2
    assert comms.bus.log.latest_sequence() == 2


@pytest.mark.parametrize("phase", ["before_bus_append", "after_bus_append_before_fsync"])
def test_sigkill_across_claim_acquisition_recovers_one_row_or_none(tmp_path, phase):
    comms, _ = _participants(tmp_path)
    code = """
import os, signal, sys
from pathlib import Path
sys.path.insert(0, sys.argv[2])
sys.path.insert(0, str(Path(sys.argv[2]).parent / 'tests'))
import agent_comms.store_files as store_files
from agent_comms.comms import Comms
from test_envelope_bus_integration import acquire_message
root = Path(sys.argv[1])
phase = sys.argv[3]
bus = root / 'bus.jsonl'
real_fsync = os.fsync

def kill_at_boundary(fd):
    name = os.readlink(f'/proc/self/fd/{fd}')
    if phase == 'before_bus_append' and '.bus_meta.json.' in name or (phase == 'after_bus_append_before_fsync' and name == str(bus) and bus.stat().st_size > 0):
        os.kill(os.getpid(), signal.SIGKILL)
    real_fsync(fd)
store_files.os.fsync = kill_at_boundary
acquire_message(Comms(root), 'alice', Path('a.py'))
"""
    source = str(Path(__file__).resolve().parents[1] / "src")
    env = os.environ.copy()
    for key in ("PI_PARENT_ID", "PI_AGENT_TAGS", "AGENT_COMMS_THREAD", "AGENT_COMMS_TAGS"):
        env.pop(key, None)
    child = subprocess.run(
        [sys.executable, "-c", code, str(comms.root), source, phase],
        env=env, capture_output=True, check=False, timeout=8,
    )
    assert child.returncode == -signal.SIGKILL, child.stderr.decode(errors="replace")
    rows = read_history(comms)
    if phase == "before_bus_append":
        assert rows == []
        acquire_message(comms, "bob", Path("a.py"))
    else:
        assert len(rows) == 1 and rows[0].claim_transition is not None
        with pytest.raises(ClaimConflict):
            acquire_message(comms, "bob", Path("a.py"))
    assert len(read_history(comms)) == 1
