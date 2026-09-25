"""Claim-envelope bus durability boundary on an initialized private root."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from threading import Barrier

import pytest

from agent_comms import declarations
from agent_comms.declarations import (
    ClaimEnvelopeUnknownError,
    Message,
    MessageType,
    RelationViolationError,
    Thread,
)
from agent_comms.envelope_claim_transitions import ClaimConflict, ClaimTransitionError
from agent_comms.exporting import WireExportFormat, WireExportLimit, WireExportScope
from agent_comms.operations import Comms

pytestmark = pytest.mark.skipif(
    sys.platform != "linux",
    reason="private claim-bus fsync/kill fixtures require Linux /var/tmp and /proc/self/fd",
)


def marked(tmp_path: Path) -> Comms:
    comms = Comms(tmp_path / "wire")
    root_id = comms.initialize_private_initial_protocol()
    assert comms.initialize_private_claim_protocol() == root_id
    marker = comms.root / "bus_meta.json"  # existing private-root marker, not claim authority
    assert json.loads(marker.read_text())["claim_envelopes_version"] == 1
    assert json.loads(marker.read_text())["wire_root_id"] == root_id
    assert marker.stat().st_mode & 0o777 == 0o600
    assert not (comms.root / ".bus.jsonl.claim-gate-v1").exists()
    return comms


def _sample_line(*, complete: bool = True) -> bytes:
    row = Message("author", "reader", "one", MessageType.INFO, seq=1).to_wire()
    return json.dumps(row).encode() + (b"\n" if complete else b"")


def test_marker_requires_fresh_private_root_and_claim_protocol(tmp_path: Path) -> None:
    ordinary = Comms(tmp_path / "ordinary")
    with pytest.raises(RelationViolationError, match="marker"):
        ordinary.initialize_private_claim_protocol()
    comms = marked(tmp_path)
    assert comms.initialize_private_claim_protocol() == (
        json.loads((comms.root / "bus_meta.json").read_text())["wire_root_id"]
    )
    assert comms.bus.full_history() == []


def test_visible_claim_protocol_flag_after_directory_fsync_error_is_resynced(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    comms = Comms(tmp_path / "wire", private_initial_writes=True, private_claim_writes=True)
    root_id = comms.initialize_private_initial_protocol()
    real_fsync = os.fsync
    failed = False

    def fsync(descriptor: int) -> None:
        nonlocal failed
        target = os.readlink(f"/proc/self/fd/{descriptor}")
        marker = comms.root / "bus_meta.json"
        if (
            not failed
            and target == str(comms.root)
            and json.loads(marker.read_text()).get("claim_envelopes_version") == 1
        ):
            failed = True
            raise OSError("directory fsync failed after visible protocol flag")
        real_fsync(descriptor)

    with monkeypatch.context() as patch:
        patch.setattr(declarations.os, "fsync", fsync)
        with pytest.raises(OSError, match="directory fsync failed"):
            comms.initialize_private_claim_protocol()
    assert failed
    assert json.loads((comms.root / "bus_meta.json").read_text())["claim_envelopes_version"] == 1
    assert comms.bus.full_history() == []  # bus lock re-fsyncs marker directory
    assert comms.initialize_private_claim_protocol() == root_id


def test_complete_visible_after_failed_fsync_must_be_resynced_before_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    comms = marked(tmp_path)
    path = comms.bus._path
    path.write_bytes(_sample_line())  # model a full visible row after failed writer fsync
    real_fsync = os.fsync
    calls: list[str] = []
    fail = True

    def fsync(descriptor: int) -> None:
        nonlocal fail
        target = os.readlink(f"/proc/self/fd/{descriptor}")
        calls.append(target)
        if target == str(path) and fail:
            fail = False
            raise OSError("simulated first bus fsync failure")
        real_fsync(descriptor)

    monkeypatch.setattr(declarations.os, "fsync", fsync)
    with pytest.raises(RelationViolationError, match="UNKNOWN"):
        comms.bus.full_history()
    assert str(path) in calls
    assert len(comms.bus.full_history()) == 1
    assert calls.count(str(path)) >= 2
    assert str(path.parent) in calls


def test_valid_json_without_newline_not_exposed_by_any_gated_read(
    tmp_path: Path,
) -> None:
    comms = marked(tmp_path)
    comms.bus._path.write_bytes(_sample_line(complete=False))
    with pytest.raises(RelationViolationError, match="Incomplete"):
        comms.bus.full_history()
    with pytest.raises(RelationViolationError, match="Incomplete"):
        comms.bus.message_by_id("receipt")
    with pytest.raises(RelationViolationError, match="Incomplete"):
        comms.bus.latest_sequence()
    with pytest.raises(RelationViolationError, match="Incomplete"):
        comms.bus.total_messages()
    with pytest.raises(RelationViolationError, match="Incomplete"):
        comms.claim_projection()
    with pytest.raises(RelationViolationError, match="Incomplete"):
        comms.relationships._recent_messages()  # independent direct bus-tail reader
    with (
        pytest.raises(RelationViolationError, match="Incomplete"),
        comms.bus.full_history_snapshot(),
    ):
        pass


def test_malformed_complete_row_is_not_an_ordinary_message(tmp_path: Path) -> None:
    comms = marked(tmp_path)
    comms.bus._path.write_bytes(b'{"seq":1,"seq":2}\n')
    with pytest.raises(RelationViolationError, match="Malformed"):
        comms.bus.full_history()


def _participants(tmp_path: Path) -> tuple[Comms, Path]:
    worktree = tmp_path / "project"
    worktree.mkdir()
    (worktree / "a.py").write_text("one")
    (worktree / "b.py").write_text("two")
    comms = Comms(tmp_path / "wire", private_initial_writes=True, private_claim_writes=True)
    for name in ("alice", "bob", "observer"):
        comms.register(Thread(name, frozenset({"team"}), str(worktree)))
    comms.initialize_private_initial_protocol()
    comms.initialize_private_claim_protocol()
    return comms, worktree


def test_one_message_is_the_only_claim_authority_and_loser_has_no_row(
    tmp_path: Path,
) -> None:
    comms, worktree = _participants(tmp_path)
    sent = comms.send_message(
        "alice", "bob", "Claim both", MessageType.HANDOFF, claims=["a.py", "b.py"]
    )
    assert sent.claim_transition is not None
    assert sent.claim_transition.claims == tuple(
        sorted((str(worktree / "a.py"), str(worktree / "b.py")))
    )
    assert sent.claim_transition.message_id == sent.message_id
    assert sent.claim_transition.seq == sent.seq
    assert comms.message_high_water() == sent.seq
    assert comms.full_history() == [sent]
    projection = comms.claim_projection()
    assert projection.get(str(worktree / "a.py")).owner == "alice"
    assert projection.get(str(worktree / "b.py")).generation == (sent.claim_transition.generation)
    with pytest.raises(ClaimConflict) as loss:
        comms.send_message(
            "bob", "alice", "Cannot claim half", MessageType.HANDOFF, claims=["a.py", "b.py"]
        )
    assert loss.value.existing.owner == "alice"
    assert comms.full_history() == [sent]
    assert comms.bus.inbox("bob")[0].claim_transition == sent.claim_transition
    fresh = Comms(comms.root)
    assert fresh.full_history() == [sent]
    marker_before = (comms.root / "bus_meta.json").read_bytes()
    with pytest.raises(RelationViolationError, match="Legacy append"):
        fresh.send_message("alice", "bob", "old writer cannot append")
    with pytest.raises(RelationViolationError, match="Private bus protocol blocks legacy deletion"):
        fresh.bus.remove_thread("alice")
    assert (comms.root / "bus_meta.json").read_bytes() == marker_before


def test_whole_set_rejects_conflict_without_partial_ownership(tmp_path: Path) -> None:
    comms, worktree = _participants(tmp_path)
    comms.send_message("alice", "bob", "Alice owns a", claims=["a.py"])
    with pytest.raises(ClaimConflict) as loss:
        comms.send_message("bob", "alice", "Bob claims both", claims=["a.py", "b.py"])
    assert loss.value.existing.owner == "alice"
    assert comms.claim_projection().get(str(worktree / "b.py")) is None
    assert len(comms.full_history()) == 1


def test_same_file_double_slash_alias_conflicts_without_second_message(
    tmp_path: Path,
) -> None:
    comms, worktree = _participants(tmp_path)
    canonical = str(worktree / "a.py")
    alias = str(worktree) + "//a.py"
    assert os.path.samefile(canonical, alias) and canonical != alias
    first = comms.send_message("alice", "bob", "Alice claims canonical", claims=[canonical])
    with pytest.raises(ClaimConflict) as conflict:
        comms.send_message("bob", "alice", "Bob claims alias", claims=[alias])
    assert conflict.value.existing.owner == "alice"
    assert comms.full_history() == [first]
    assert dict(comms.claim_projection()) == {canonical: conflict.value.existing}


def test_durable_raw_alias_claim_cannot_enter_guarded_public_or_projection(
    tmp_path: Path,
) -> None:
    """An old/noncanonical claim row must never acquire a second identity."""
    comms, worktree = _participants(tmp_path)
    first = comms.send_message("alice", "bob", "Canonical claim", claims=["a.py"])
    alias = str(worktree) + "//a.py"
    assert os.path.samefile(str(worktree / "a.py"), alias)
    second = Message("bob", "alice", "Invalid raw alias", MessageType.INFO, seq=first.seq + 1)
    record = second.to_wire()
    record["claim_transition"] = {
        "owner": "bob",
        "incarnation": str(comms.registry.require("bob").created_at),
        "seq": second.seq,
        "message_id": second.message_id,
        "claims": [alias],
        "releases": [],
        "generation": "b" * 32,
    }
    with declarations._store_lock(comms.bus._path):
        metadata = comms.bus._private_marker_unlocked()
        comms.bus._append_private_unlocked(metadata, record)
    with pytest.raises(RelationViolationError, match="Malformed claim envelope"):
        comms.full_history()
    with pytest.raises(RelationViolationError, match="Malformed claim envelope"):
        comms.claim_projection()
    with pytest.raises(RelationViolationError, match="Malformed claim envelope"):
        comms.bus.message_by_id(second.message_id)


def test_release_requires_exact_owner_and_preserves_generation(
    tmp_path: Path,
) -> None:
    comms, worktree = _participants(tmp_path)
    claimed = comms.send_message("alice", "bob", "I own a", claims=["a.py"])
    with pytest.raises(ClaimTransitionError, match="exact owner"):
        comms.send_message("bob", "alice", "Wrong release", releases=["a.py"])
    assert len(comms.full_history()) == 1
    released = comms.send_message("alice", "bob", "Released", releases=["a.py"])
    assert comms.claim_projection().get(str(worktree / "a.py")) is None
    assert released.claim_transition is not None
    assert claimed.claim_transition is not None
    assert released.claim_transition.releases[0].generation == (claimed.claim_transition.generation)
    taken = comms.send_message("bob", "alice", "Next owner", claims=["a.py"])
    assert len(comms.full_history()) == 3
    assert taken.claim_transition is not None
    assert taken.claim_transition.owner == "bob"
    assert taken.claim_transition.generation != claimed.claim_transition.generation


def test_public_rename_preserves_claim_release_then_new_owner_wins(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    comms, worktree = _participants(tmp_path)
    path = str(worktree / "a.py")
    claimed = comms.send_message("alice", "bob", "Claim before rename", claims=["a.py"])
    original = comms.registry.require("alice")
    monkeypatch.setenv("PI_AGENT_ID", "alice")
    result = comms.rename_self("alice-new")
    assert (result.previous, result.current, result.changed) == ("alice", "alice-new", True)
    for previous, current in (("alice-new", "alice-next"), ("alice-next", "alice-final")):
        monkeypatch.setenv("PI_AGENT_ID", previous)
        renamed = comms.rename_self(current)
        assert (renamed.previous, renamed.current, renamed.changed) == (previous, current, True)
    assert comms.registry.require("alice-final").created_at == original.created_at
    for alias in ("alice", "alice-new", "alice-next"):
        assert comms.registry.require(alias).name == "alice-final"
        assert comms.registry.name_reserved(alias)
    next_thread = comms.claim_thread("alice", tags=frozenset({"team"}), worktree=str(worktree))
    assert next_thread.name != "alice" and next_thread.created_at != original.created_at

    reopened = Comms(comms.root)
    assert reopened.claim_projection()[path].owner == "alice"
    with pytest.raises(ClaimTransitionError, match="exact owner"):
        reopened.send_message(
            "bob", "alice-final", "Cannot take renamed owner claim", releases=["a.py"]
        )
    with pytest.raises(ClaimTransitionError, match="exact owner"):
        reopened.send_message(
            next_thread.name, "bob", "New incarnation cannot release", releases=["a.py"]
        )
    assert len(reopened.full_history()) == 1
    released = reopened.send_message(
        "alice-final", "bob", "Release after rename", releases=["a.py"]
    )
    assert released.claim_transition is not None
    assert claimed.claim_transition is not None
    assert released.claim_transition.owner == "alice-final"
    assert released.claim_transition.incarnation == claimed.claim_transition.incarnation
    assert released.claim_transition.releases[0].generation == claimed.claim_transition.generation
    assert path not in reopened.claim_projection()

    taken = reopened.send_message("bob", "alice-final", "New owner", claims=["a.py"])
    assert reopened.claim_projection()[path].owner == "bob"
    assert taken.claim_transition is not None
    assert taken.claim_transition.generation != claimed.claim_transition.generation
    with pytest.raises(ClaimTransitionError, match="exact owner"):
        reopened.send_message("alice", "bob", "Old alias cannot release Bob", releases=["a.py"])
    assert len(reopened.full_history()) == 3
    assert Comms(comms.root).claim_projection()[path].owner == "bob"


def test_rename_back_to_own_alias_preserves_claim_release(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    comms, worktree = _participants(tmp_path)
    path = str(worktree / "a.py")
    claimed = comms.send_message("alice", "bob", "Claim before rename", claims=["a.py"])
    assert claimed.claim_transition is not None
    monkeypatch.setenv("PI_AGENT_ID", "alice")
    assert comms.rename_self("alice-new").changed
    monkeypatch.setenv("PI_AGENT_ID", "alice-new")

    assert comms.rename_self("alice").current == "alice"

    reopened = Comms(comms.root)
    assert reopened.registry.snapshot().aliases == {"alice-new": "alice"}
    assert reopened.claim_projection()[path].owner == "alice"
    released = reopened.send_message("alice", "bob", "Release after rename back", releases=["a.py"])
    assert released.claim_transition is not None
    assert released.claim_transition.incarnation == claimed.claim_transition.incarnation
    assert path not in reopened.claim_projection()


def test_same_tick_new_owner_cannot_share_live_claim_release_authority(tmp_path: Path) -> None:
    comms, worktree = _participants(tmp_path)
    claimed = comms.send_message("alice", "bob", "Claim a", claims=["a.py"])
    alice = comms.registry.require("alice")
    with pytest.raises(RelationViolationError, match="creation identities collide"):
        comms.register(
            Thread(
                "same-tick-peer",
                frozenset({"team"}),
                str(worktree),
                created_at=alice.created_at,
            )
        )
    assert "same-tick-peer" not in comms.registry
    assert comms.full_history() == [claimed]
    assert comms.claim_projection()[str(worktree / "a.py")].owner == "alice"
    comms.send_message("alice", "bob", "Legitimate release", releases=["a.py"])
    assert str(worktree / "a.py") not in comms.claim_projection()


def test_legacy_colliding_creation_id_blocks_claim_write_before_append(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    comms, worktree = _participants(tmp_path)
    claimed = comms.send_message("alice", "bob", "Claim a", claims=["a.py"])
    actual_threads = comms.registry.all_threads

    def old_registry_snapshot() -> dict[str, Thread]:
        threads = dict(actual_threads())
        threads["bob"] = replace(threads["bob"], created_at=threads["alice"].created_at)
        return threads

    monkeypatch.setattr(comms.registry, "all_threads", old_registry_snapshot)
    with pytest.raises(RelationViolationError, match="creation identities collide"):
        comms.send_message("bob", "alice", "Cannot release under duplicate ID", releases=["a.py"])
    assert comms.full_history() == [claimed]


@pytest.mark.parametrize("suffix", ["//a.py", "/./a.py"])
def test_owner_release_alias_rejected_before_it_can_relinquish_claim(
    tmp_path: Path, suffix: str
) -> None:
    comms, worktree = _participants(tmp_path)
    claimed = comms.send_message("alice", "bob", "Claim canonical", claims=["a.py"])
    canonical = str(worktree / "a.py")
    raw_alias = str(worktree) + suffix
    assert os.path.samefile(canonical, raw_alias)
    with pytest.raises(RelationViolationError, match="canonical spelling"):
        comms.send_message("alice", "bob", "Alias release denied", releases=[raw_alias])
    assert comms.full_history() == [claimed]
    assert comms.claim_projection()[canonical].owner == "alice"
    released = comms.send_message("alice", "bob", "Exact release", releases=[canonical])
    assert released.claim_transition is not None
    assert comms.claim_projection().get(canonical) is None


def test_owner_can_release_exact_claim_after_file_deletion(tmp_path: Path) -> None:
    comms, worktree = _participants(tmp_path)
    comms.send_message("alice", "bob", "Claim before deletion", claims=["a.py"])
    (worktree / "a.py").unlink()
    comms.send_message("alice", "bob", "Release deleted file", releases=["a.py"])
    assert comms.claim_projection().get(str(worktree / "a.py")) is None
    assert len(comms.full_history()) == 2


def test_concurrent_whole_set_exactly_one_winner(tmp_path: Path) -> None:
    comms, _ = _participants(tmp_path)
    simultaneous = Barrier(2)

    def attempt(name: str) -> str:
        owner = Comms(comms.root, private_claim_writes=True)
        simultaneous.wait(timeout=3)
        try:
            owner.send_message(name, "observer", "Competing claims", claims=["a.py", "b.py"])
        except ClaimConflict:
            return "lost"
        return "won"

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = tuple(pool.map(attempt, ("alice", "bob")))
    assert sorted(results) == ["lost", "won"]
    assert len(comms.full_history()) == 1


@pytest.mark.parametrize("fail_at", ["bus", "directory"])
def test_failed_fsync_complete_visible_row_is_unknown_not_replayed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fail_at: str
) -> None:
    comms, _ = _participants(tmp_path)
    real_fsync = os.fsync
    failed = False

    def fsync(descriptor: int) -> None:
        nonlocal failed
        target = os.readlink(f"/proc/self/fd/{descriptor}")
        bus = comms.bus._path
        trigger = (
            target == str(bus)
            if fail_at == "bus"
            else (target == str(bus.parent) and bus.exists() and bus.stat().st_size > 0)
        )
        if trigger and not failed:
            failed = True
            raise OSError("injected claim-row fsync failure")
        real_fsync(descriptor)

    monkeypatch.setattr(declarations.os, "fsync", fsync)
    with pytest.raises(ClaimEnvelopeUnknownError, match="UNKNOWN"):
        comms.send_message("alice", "bob", "Maybe claimed", claims=["a.py"])
    assert failed
    # No retry of Alice's uncertain send; a separate guarded reader resyncs
    # the complete visible row before either observing it or rejecting Bob.
    messages = comms.full_history()
    assert len(messages) == 1
    assert messages[0].claim_transition is not None
    with pytest.raises(ClaimConflict):
        comms.send_message("bob", "alice", "Conflict", claims=["a.py"])
    assert comms.full_history() == messages


def test_fsynced_existing_bus_row_survives_single_uncommitted_marker_rename_loss(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Only a marker rename rolls back; the existing bus inode's append survives."""
    comms, worktree = _participants(tmp_path)
    (worktree / "c.py").write_text("third")
    first = comms.send_message("alice", "bob", "Alice owns a", claims=["a.py"])
    marker = comms.root / "bus_meta.json"
    bus = comms.bus._path
    old_marker = marker.read_bytes()
    old_size = bus.stat().st_size
    real_fsync = os.fsync
    bus_fsynced = False
    failed = False

    def fail_final_directory_fsync(descriptor: int) -> None:
        nonlocal bus_fsynced, failed
        target = os.readlink(f"/proc/self/fd/{descriptor}")
        if target == str(bus) and bus.stat().st_size > old_size:
            bus_fsynced = True
        if target == str(comms.root) and bus_fsynced and not failed:
            failed = True
            raise OSError("directory fsync after existing-inode append")
        real_fsync(descriptor)

    with monkeypatch.context() as patch:
        patch.setattr(declarations.os, "fsync", fail_final_directory_fsync)
        with pytest.raises(ClaimEnvelopeUnknownError, match="UNKNOWN"):
            comms.send_message("bob", "alice", "Bob owns b", claims=["b.py"])
    assert bus_fsynced and failed
    assert bus.stat().st_size > old_size
    assert len(comms.full_history()) == 2
    # Model an ordinary crash reverting ONLY the unsynced atomic marker rename.
    # The existing bus inode and its appended row were already fsynced.
    marker.write_bytes(old_marker)
    with marker.open("rb") as stream:
        real_fsync(stream.fileno())
    directory_fd = os.open(comms.root, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        real_fsync(directory_fd)
    finally:
        os.close(directory_fd)

    reopened = Comms(comms.root, private_claim_writes=True)
    committed = reopened.full_history()
    assert len(committed) == 2 and committed[0] == first
    assert committed[1].seq == 2
    projection = reopened.claim_projection()
    assert projection[str(worktree / "a.py")].owner == "alice"
    assert projection[str(worktree / "b.py")].owner == "bob"
    with pytest.raises(ClaimConflict):
        reopened.send_message("alice", "bob", "Loser never publishes", claims=["b.py"])
    assert len(reopened.full_history()) == 2
    third = reopened.send_message("observer", "alice", "Next distinct claim", claims=["c.py"])
    assert third.seq == 3
    assert len(reopened.full_history()) == 3
    assert reopened.claim_projection()[str(worktree / "c.py")].owner == "observer"


def test_oversize_transition_cannot_brick_a_successfully_published_root(
    tmp_path: Path,
) -> None:
    comms, worktree = _participants(tmp_path)
    directory = worktree
    for number in range(10):
        directory = directory / ("d" * 180 + str(number))
        directory.mkdir()
    resources = []
    for number in range(10):
        resource = directory / f"{number}.py"
        resource.write_text("ordinary file")
        resources.append(str(resource.relative_to(worktree)))
    with pytest.raises(RelationViolationError, match="malformed"):
        comms.send_message("alice", "bob", "Oversize claim", claims=resources)
    assert comms.full_history() == []
    assert dict(comms.claim_projection()) == {}
    committed = comms.send_message("alice", "bob", "Safe claim", claims=["a.py"])
    assert comms.full_history() == [committed]
    assert comms.bus.message_by_id(committed.message_id) == committed
    assert comms.claim_projection()[str(worktree / "a.py")].owner == "alice"
    destination = tmp_path / "safe-export.jsonl"
    receipt = comms.export_wire(
        destination,
        format=WireExportFormat.JSONL,
        scope=WireExportScope.everything(),
        limit=WireExportLimit.full(),
    )
    exported = [json.loads(line) for line in destination.read_text().splitlines()]
    assert receipt.exported_messages == 1
    assert len(exported) == 2 and exported[1]["record"] == "message"
    assert exported[1]["message"] == committed.to_wire()


def test_invalid_claim_record_cannot_be_exposed_as_plain_message(tmp_path: Path) -> None:
    comms, _ = _participants(tmp_path)
    public = Message("alice", "bob", "attempt", MessageType.INFO, seq=1).to_wire()
    public["claim_transition"] = {"claims": ["/tmp/a.py"]}
    comms.bus._path.write_bytes(json.dumps(public).encode() + b"\n")
    with pytest.raises(RelationViolationError, match="Malformed claim"):
        comms.bus.full_history()


def _private_verified_rows(comms: Comms) -> list[tuple[object, object, object]]:
    with declarations._store_lock(comms.bus._path):
        metadata = comms.bus._private_marker_unlocked()
        return list(comms.bus._verified_private_rows_unlocked(metadata))


def _ordinary_locked_iterator(comms: Comms) -> list[Message]:
    with declarations._store_lock(comms.bus._path):
        return list(comms.bus._iter_log_unlocked())


def _ordinary_snapshot(comms: Comms) -> list[object]:
    with comms.bus._record_snapshot() as (_, records):
        return list(records)


def test_claim_bearing_visible_failed_fsync_all_reader_routes_block_then_resync(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    comms, worktree = _participants(tmp_path)
    bus = comms.bus._path
    real_fsync = os.fsync
    phase = "writer"
    fsyncs = 0

    def fail_fsync(descriptor: int) -> None:
        nonlocal fsyncs
        target = os.readlink(f"/proc/self/fd/{descriptor}")
        if target == str(bus):
            fsyncs += 1
            if phase != "allow":
                raise OSError("claim row visible but not durable")
        real_fsync(descriptor)

    monkeypatch.setattr(declarations.os, "fsync", fail_fsync)
    with pytest.raises(ClaimEnvelopeUnknownError, match="UNKNOWN"):
        comms.send_message("alice", "bob", "Uncertain claimed envelope", claims=["a.py"])
    assert bus.read_bytes().endswith(b"\n")
    raw = json.loads(bus.read_text().splitlines()[0])
    assert raw["claim_transition"]["claims"] == [str(worktree / "a.py")]
    receipt = raw["id"]
    phase = "reader_blocked"
    denied = (
        lambda: comms.bus.message_by_id(receipt),
        lambda: _ordinary_locked_iterator(comms),
        lambda: _ordinary_snapshot(comms),
        lambda: comms.full_history(),
        lambda: comms.bus.total_messages(),
        lambda: comms.bus.latest_sequence(),
        lambda: comms.claim_projection(),
        lambda: _private_verified_rows(comms),
    )
    for read in denied:
        with pytest.raises(RelationViolationError, match="UNKNOWN"):
            read()
    assert fsyncs == 1 + len(denied)
    phase = "allow"
    message = comms.bus.message_by_id(receipt)
    assert message is not None and message.claim_transition is not None
    assert _ordinary_locked_iterator(comms) == [message]
    assert _ordinary_snapshot(comms)[0][0] == message
    assert comms.full_history() == [message]
    assert comms.bus.total_messages() == 1
    assert comms.bus.latest_sequence() == message.seq
    assert comms.claim_projection()[str(worktree / "a.py")].owner == "alice"
    assert len(_private_verified_rows(comms)) == 1


def test_claim_bearing_json_valid_no_newline_never_exposed(tmp_path: Path) -> None:
    comms, _ = _participants(tmp_path)
    committed = comms.send_message("alice", "bob", "Original claim", claims=["a.py"])
    bus = comms.bus._path
    assert bus.read_bytes().endswith(b"\n")
    bus.write_bytes(bus.read_bytes()[:-1])  # crash left an incomplete but JSON-valid tail
    denied = (
        lambda: comms.bus.message_by_id(committed.message_id),
        lambda: _ordinary_locked_iterator(comms),
        lambda: _ordinary_snapshot(comms),
        lambda: comms.full_history(),
        lambda: comms.bus.total_messages(),
        lambda: comms.bus.latest_sequence(),
        lambda: comms.claim_projection(),
        lambda: _private_verified_rows(comms),
    )
    for read in denied:
        with pytest.raises(RelationViolationError, match="Incomplete"):
            read()


def test_reserved_metadata_sequence_is_not_a_committed_message(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    comms, _ = _participants(tmp_path)
    bus = comms.bus._path
    real_open = os.open

    def fail_before_append(
        path: str | os.PathLike[str], flags: int, *args: object, **kwargs: object
    ) -> int:
        if str(path) == str(bus) and flags & os.O_APPEND:
            raise OSError("injected failure after metadata reservation")
        return real_open(path, flags, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(declarations.os, "open", fail_before_append)
        with pytest.raises(ClaimEnvelopeUnknownError, match="UNKNOWN"):
            comms.send_message("alice", "bob", "Never appended", claims=["a.py"])
    assert json.loads((comms.root / "bus_meta.json").read_text())["last_seq"] == 1
    assert comms.full_history() == []
    assert comms.message_high_water() == 0
    with comms.bus.full_history_snapshot() as (through, rows):
        assert through == 0 and list(rows) == []
    committed = comms.send_message("bob", "alice", "Next safe claim", claims=["a.py"])
    assert committed.seq == 2  # reserved gaps are never reused
    assert comms.message_high_water() == 2


@pytest.mark.parametrize("phase", ["before_bus_append", "after_bus_append_before_fsync"])
def test_sigkill_across_claim_send_recovers_one_row_or_none(tmp_path: Path, phase: str) -> None:
    comms, _ = _participants(tmp_path)
    code = r"""
import os, signal, sys
from pathlib import Path
sys.path.insert(0, sys.argv[2])
from agent_comms import declarations
from agent_comms.operations import Comms
root = Path(sys.argv[1]); phase = sys.argv[3]
bus = root / 'bus.jsonl'
real_fsync = os.fsync
def kill_at_boundary(fd):
    name = os.readlink(f'/proc/self/fd/{fd}')
    if (phase == 'before_bus_append' and '.bus_meta.json.' in name
        or phase == 'after_bus_append_before_fsync' and name == str(bus)):
        os.kill(os.getpid(), signal.SIGKILL)
    real_fsync(fd)
declarations.os.fsync = kill_at_boundary
Comms(root, private_claim_writes=True).send_message(
    'alice', 'bob', 'Interrupted claim', claims=['a.py'])
"""
    source = str(Path(__file__).resolve().parents[1] / "src")
    env = os.environ.copy()
    for key in (
        "PI_AGENT_ID",
        "PI_PARENT_ID",
        "PI_AGENT_TAGS",
        "AGENT_COMMS_THREAD",
        "AGENT_COMMS_TAGS",
    ):
        env.pop(key, None)
    child = subprocess.run(
        [sys.executable, "-c", code, str(comms.root), source, phase],
        env=env,
        capture_output=True,
        check=False,
        timeout=8,
    )
    assert child.returncode == -signal.SIGKILL, child.stderr.decode(errors="replace")
    rows = comms.full_history()  # guarded reader fsyncs a complete visible row
    if phase == "before_bus_append":
        assert rows == []
        comms.send_message("bob", "alice", "First durable claim", claims=["a.py"])
    else:
        assert len(rows) == 1 and rows[0].claim_transition is not None
        with pytest.raises(ClaimConflict):
            comms.send_message("bob", "alice", "No replay or duplicate claim", claims=["a.py"])
    assert len(comms.full_history()) == 1
