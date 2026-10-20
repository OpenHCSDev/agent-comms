"""Claim-envelope bus durability boundary on an initialized private root."""

from __future__ import annotations

from bus_history import read_history
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

import agent_comms.store_files as store_files
from agent_comms.comms import Comms
from agent_comms.envelope_claim_transitions import (
    ClaimConflict,
    ClaimTransitionError,
    ExistingFileClaim,
)
from agent_comms.errors import ClaimEnvelopeUnknownError, RelationViolationError, StoredHistoryUnreadable
from agent_comms.field_codec import MalformedRecord
from agent_comms.exporting import (
    EverythingScope,
    FullLimit,
    JsonlFormat,
)
from agent_comms.messages import Message, HandoffKind, InfoKind
from agent_comms.message_reference import MessageReference
from agent_comms.threads import Thread

pytestmark = pytest.mark.skipif(
    sys.platform != "linux",
    reason="private claim-bus fsync/kill fixtures require Linux /var/tmp and /proc/self/fd",
)


def marked(tmp_path: Path) -> Comms:
    comms = Comms(tmp_path / "wire")
    root_id = comms.messaging.initialize_private_initial_protocol()
    marker = comms.root / "bus_meta.json"  # existing private-root marker, not claim authority
    assert json.loads(marker.read_text())["claim_envelopes_version"] == 1
    assert json.loads(marker.read_text())["wire_root_id"] == root_id
    assert marker.stat().st_mode & 0o777 == 0o600
    assert not (comms.root / ".bus.jsonl.claim-gate-v1").exists()
    return comms


def _sample_line(*, complete: bool = True) -> bytes:
    row = Message("author", "reader", "one", InfoKind, seq=1).to_wire()
    return json.dumps(row).encode() + (b"\n" if complete else b"")






def test_complete_visible_after_failed_fsync_must_be_resynced_before_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    comms = marked(tmp_path)
    path = comms.bus.log.path
    for name in ("author", "reader"):
        comms.registry.declare(comms.registry.new_thread(name, frozenset(), str(tmp_path)))
    comms.messaging.send("author", "reader", "one").message
    # Rewrite the actual canonical row without fsync, modeling the failed writer.
    path.write_bytes(path.read_bytes())
    path.chmod(0o600)
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

    monkeypatch.setattr(store_files.os, "fsync", fsync)
    with pytest.raises(RelationViolationError, match="UNKNOWN"):
        read_history(comms)
    assert str(path) in calls
    assert len(read_history(comms)) == 1
    assert calls.count(str(path)) >= 2
    assert str(path.parent) in calls


def test_valid_json_without_newline_not_exposed_by_any_gated_read(
    tmp_path: Path,
) -> None:
    comms = marked(tmp_path)
    comms.bus.log.path.write_bytes(_sample_line(complete=False))
    with pytest.raises(RelationViolationError, match="Incomplete"):
        read_history(comms)
    with pytest.raises(RelationViolationError, match="Incomplete"):
        comms.bus.log.messages_for_references((MessageReference(1, "acknowledgement"),))
    with pytest.raises(RelationViolationError, match="Incomplete"):
        comms.bus.log.latest_sequence()
    with (
        pytest.raises(RelationViolationError, match="Incomplete"),
        comms.bus.log.full_history_snapshot() as (_, messages),
    ):
        list(messages)
    with pytest.raises(RelationViolationError, match="Incomplete"):
        comms.bus.log.claim_projection()
    with pytest.raises(RelationViolationError, match="Incomplete"):
        comms.relationships._recent_messages()  # independent direct bus-tail reader


def test_malformed_complete_row_is_not_an_ordinary_message(tmp_path: Path) -> None:
    comms = marked(tmp_path)
    comms.bus.log.path.write_bytes(b'{"seq":1,"seq":2}\n')
    with pytest.raises(StoredHistoryUnreadable, match="Malformed public bus row") as malformed:
        read_history(comms)
    assert isinstance(malformed.value.__cause__, ValueError)


def _participants(tmp_path: Path) -> tuple[Comms, Path]:
    worktree = tmp_path / "project"
    worktree.mkdir()
    (worktree / "a.py").write_text("one")
    (worktree / "b.py").write_text("two")
    comms = Comms(tmp_path / "wire")
    for name in ("alice", "bob", "observer"):
        comms.registry.declare(comms.registry.new_thread(name, frozenset({'team'}), str(worktree)))
    comms.messaging.initialize_private_initial_protocol()
    return comms, worktree


def test_one_message_is_the_only_claim_authority_and_loser_has_no_row(
    tmp_path: Path,
) -> None:
    comms, worktree = _participants(tmp_path)
    sent = comms.messaging.send(
        "alice",
        "bob",
        "Claim both",
        HandoffKind,
        claims=[ExistingFileClaim(Path("a.py")), ExistingFileClaim(Path("b.py"))],
    ).message
    assert sent.claim_transition is not None
    assert sent.claim_transition.claims == tuple(
        sorted((str(worktree / "a.py"), str(worktree / "b.py")))
    )
    assert sent.claim_transition.message_id == sent.message_id
    assert sent.claim_transition.seq == sent.seq
    assert comms.bus.log.latest_sequence() == sent.seq
    # Durable claim history keeps its external shape, including absent claim_scope.
    stored = json.loads(comms.bus.log.path.read_text().splitlines()[0])
    assert stored["claim_transition"] == {
        "owner": "alice",
        "incarnation": sent.claim_transition.incarnation,
        "seq": sent.seq,
        "message_id": sent.message_id,
        "claims": list(sent.claim_transition.claims),
        "releases": [],
        "generation": sent.claim_transition.generation,
    }
    from agent_comms.field_codec import FieldCodec
    from agent_comms.routing import TurnRouting

    reference = MessageReference(sent.seq, sent.message_id)
    routing = TurnRouting((reference,), None)
    assert FieldCodec.encode(routing)["requests"] == [
        {"seq": sent.seq, "message_id": sent.message_id}
    ]
    comms.transcripts.routes.record("session", ("entry",), routing)
    with Comms(comms.root).transcripts.routes.for_session("session") as routes:
        assert routes.get("entry") == routing
    assert read_history(comms) == [sent]
    projection = comms.bus.log.claim_projection()
    assert projection.get(str(worktree / "a.py")).owner == "alice"
    assert projection.get(str(worktree / "b.py")).generation == (sent.claim_transition.generation)
    with pytest.raises(ClaimConflict) as loss:
        comms.messaging.send(
            "bob",
            "alice",
            "Cannot claim half",
            HandoffKind,
            claims=[ExistingFileClaim(Path("a.py")), ExistingFileClaim(Path("b.py"))],
        ).message
    assert loss.value.existing.owner == "alice"
    assert read_history(comms) == [sent]
    assert comms.bus.inbox("bob")[0].claim_transition == sent.claim_transition
    fresh = Comms(comms.root)
    assert read_history(fresh) == [sent]


def test_absolute_claim_outside_worktree_uses_one_global_key(tmp_path: Path) -> None:
    comms, worktree = _participants(tmp_path)
    outside = tmp_path / "shared" / "module.py"
    outside.parent.mkdir()
    outside.write_text("shared")
    claimed = comms.messaging.send(
        "alice", "bob", "Claim shared file", claims=[ExistingFileClaim(outside)]
    ).message
    assert claimed.claim_transition is not None
    assert claimed.claim_transition.claims == (str(outside),)
    projection = Comms(comms.root).bus.log.claim_projection()
    assert list(projection) == [str(outside)]
    assert projection[str(outside)].owner == "alice"
    with pytest.raises(ClaimConflict):
        comms.messaging.send(
            "bob", "alice", "Competing shared claim", claims=[ExistingFileClaim(outside)]
        ).message
    comms.messaging.send("alice", "bob", "Release shared file", releases=[outside]).message
    assert not comms.bus.log.claim_projection()
    assert worktree != outside.parent


def test_whole_set_rejects_conflict_without_partial_ownership(tmp_path: Path) -> None:
    comms, worktree = _participants(tmp_path)
    comms.messaging.send(
        "alice", "bob", "Alice owns a", claims=[ExistingFileClaim(Path("a.py"))]
    ).message
    with pytest.raises(ClaimConflict) as loss:
        comms.messaging.send(
            "bob",
            "alice",
            "Bob claims both",
            claims=[ExistingFileClaim(Path("a.py")), ExistingFileClaim(Path("b.py"))],
        ).message
    assert loss.value.existing.owner == "alice"
    assert comms.bus.log.claim_projection().get(str(worktree / "b.py")) is None
    assert len(read_history(comms)) == 1


def test_same_file_double_slash_alias_conflicts_without_second_message(
    tmp_path: Path,
) -> None:
    comms, worktree = _participants(tmp_path)
    canonical = str(worktree / "a.py")
    alias = str(worktree) + "//a.py"
    assert os.path.samefile(canonical, alias) and canonical != alias
    first = comms.messaging.send(
        "alice", "bob", "Alice claims canonical", claims=[ExistingFileClaim(Path(canonical))]
    ).message
    with pytest.raises(ClaimConflict) as conflict:
        comms.messaging.send(
            "bob", "alice", "Bob claims alias", claims=[ExistingFileClaim(Path(alias))]
        ).message
    assert conflict.value.existing.owner == "alice"
    assert read_history(comms) == [first]
    assert dict(comms.bus.log.claim_projection()) == {canonical: conflict.value.existing}


def test_raw_alias_claim_is_refused_before_append_or_projection(
    tmp_path: Path,
) -> None:
    """A noncanonical claim row never enters the durable bus."""
    comms, worktree = _participants(tmp_path)
    first = comms.messaging.send(
        "alice", "bob", "Canonical claim", claims=[ExistingFileClaim(Path("a.py"))]
    ).message
    alias = str(worktree) + "//a.py"
    assert os.path.samefile(str(worktree / "a.py"), alias)
    second = Message("bob", "alice", "Invalid raw alias", InfoKind, seq=first.seq + 1)
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
    with store_files._store_lock(comms.bus.log.path):
        metadata = comms.bus.log.private_marker_unlocked()
        with pytest.raises(ValueError, match="canonical absolute path"):
            comms.bus.log.append_private_unlocked(metadata, record)
    assert read_history(comms) == [first]
    assert list(comms.bus.log.claim_projection()) == [str(worktree / "a.py")]
    with pytest.raises(RelationViolationError, match="not its original source"):
        comms.bus.log.messages_for_references((second.reference,))


def test_release_requires_exact_owner_and_preserves_generation(
    tmp_path: Path,
) -> None:
    comms, worktree = _participants(tmp_path)
    claimed = comms.messaging.send(
        "alice", "bob", "I own a", claims=[ExistingFileClaim(Path("a.py"))]
    ).message
    with pytest.raises(ClaimTransitionError, match="exact owner"):
        comms.messaging.send("bob", "alice", "Wrong release", releases=["a.py"]).message
    assert len(read_history(comms)) == 1
    released = comms.messaging.send("alice", "bob", "Released", releases=["a.py"]).message
    assert comms.bus.log.claim_projection().get(str(worktree / "a.py")) is None
    assert released.claim_transition is not None
    assert claimed.claim_transition is not None
    assert released.claim_transition.releases[0].generation == (claimed.claim_transition.generation)
    taken = comms.messaging.send(
        "bob", "alice", "Next owner", claims=[ExistingFileClaim(Path("a.py"))]
    ).message
    assert len(read_history(comms)) == 3
    assert taken.claim_transition is not None
    assert taken.claim_transition.owner == "bob"
    assert taken.claim_transition.generation != claimed.claim_transition.generation


def test_public_rename_preserves_claim_release_then_new_owner_wins(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    comms, worktree = _participants(tmp_path)
    path = str(worktree / "a.py")
    claimed = comms.messaging.send(
        "alice", "bob", "Claim before rename", claims=[ExistingFileClaim(Path("a.py"))]
    ).message
    original = comms.registry.require("alice")
    monkeypatch.setenv("AGENT_COMMS_THREAD", "alice")
    from agent_comms.tool_caller import ThreadCaller

    result = comms.threads.rename_self("alice-new", caller=ThreadCaller("alice"))
    assert (result.previous, result.current, result.changed) == ("alice", "alice-new", True)
    for previous, current in (("alice-new", "alice-next"), ("alice-next", "alice-final")):
        monkeypatch.setenv("AGENT_COMMS_THREAD", previous)
        renamed = comms.threads.rename_self(current, caller=ThreadCaller(previous))
        assert (renamed.previous, renamed.current, renamed.changed) == (previous, current, True)
    assert comms.registry.require("alice-final").created_at == original.created_at
    for alias in ("alice", "alice-new", "alice-next"):
        assert comms.registry.require(alias).name == "alice-final"
        assert comms.registry.snapshot().taken(alias)
    next_thread = comms.threads.claim_thread(
        "alice", tags=frozenset({"team"}), worktree=str(worktree)
    )
    assert next_thread.name != "alice" and next_thread.created_at != original.created_at

    reopened = Comms(comms.root)
    assert reopened.bus.log.claim_projection()[path].owner == "alice"
    with pytest.raises(ClaimTransitionError, match="exact owner"):
        reopened.messaging.send(
            "bob", "alice-final", "Cannot take renamed owner claim", releases=["a.py"]
        ).message
    with pytest.raises(ClaimTransitionError, match="exact owner"):
        reopened.messaging.send(
            next_thread.name, "bob", "New incarnation cannot release", releases=["a.py"]
        ).message
    assert len(read_history(reopened)) == 1
    released = reopened.messaging.send(
        "alice-final", "bob", "Release after rename", releases=["a.py"]
    ).message
    assert released.claim_transition is not None
    assert claimed.claim_transition is not None
    assert released.claim_transition.owner == "alice-final"
    assert released.claim_transition.incarnation == claimed.claim_transition.incarnation
    assert released.claim_transition.releases[0].generation == claimed.claim_transition.generation
    assert path not in reopened.bus.log.claim_projection()

    taken = reopened.messaging.send(
        "bob", "alice-final", "New owner", claims=[ExistingFileClaim(Path("a.py"))]
    ).message
    assert reopened.bus.log.claim_projection()[path].owner == "bob"
    assert taken.claim_transition is not None
    assert taken.claim_transition.generation != claimed.claim_transition.generation
    with pytest.raises(ClaimTransitionError, match="exact owner"):
        reopened.messaging.send(
            "alice", "bob", "Old alias cannot release Bob", releases=["a.py"]
        ).message
    assert len(read_history(reopened)) == 3
    assert Comms(comms.root).bus.log.claim_projection()[path].owner == "bob"


def test_rename_back_to_own_alias_preserves_claim_release(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    comms, worktree = _participants(tmp_path)
    path = str(worktree / "a.py")
    claimed = comms.messaging.send(
        "alice", "bob", "Claim before rename", claims=[ExistingFileClaim(Path("a.py"))]
    ).message
    assert claimed.claim_transition is not None
    monkeypatch.setenv("AGENT_COMMS_THREAD", "alice")
    from agent_comms.tool_caller import ThreadCaller

    assert comms.threads.rename_self("alice-new", caller=ThreadCaller("alice")).changed
    monkeypatch.setenv("AGENT_COMMS_THREAD", "alice-new")

    assert comms.threads.rename_self("alice", caller=ThreadCaller("alice-new")).current == "alice"

    reopened = Comms(comms.root)
    assert reopened.registry.snapshot().aliases == {"alice-new": "alice"}
    assert reopened.bus.log.claim_projection()[path].owner == "alice"
    released = reopened.messaging.send(
        "alice", "bob", "Release after rename back", releases=["a.py"]
    ).message
    assert released.claim_transition is not None
    assert released.claim_transition.incarnation == claimed.claim_transition.incarnation
    assert path not in reopened.bus.log.claim_projection()


def test_same_tick_new_owner_cannot_share_live_claim_release_authority(tmp_path: Path) -> None:
    comms, worktree = _participants(tmp_path)
    claimed = comms.messaging.send(
        "alice", "bob", "Claim a", claims=[ExistingFileClaim(Path("a.py"))]
    ).message
    alice = comms.registry.require("alice")
    with pytest.raises(RelationViolationError, match="creation identities collide"):
        comms.registry.declare(
            comms.registry.new_thread('same-tick-peer', frozenset({'team'}), str(worktree), created_at=alice.created_at)
        )
    assert "same-tick-peer" not in comms.registry
    assert read_history(comms) == [claimed]
    assert comms.bus.log.claim_projection()[str(worktree / "a.py")].owner == "alice"
    comms.messaging.send("alice", "bob", "Legitimate release", releases=["a.py"]).message
    assert str(worktree / "a.py") not in comms.bus.log.claim_projection()


def test_saved_colliding_creation_id_blocks_claim_write_before_append(tmp_path: Path) -> None:
    comms, worktree = _participants(tmp_path)
    claimed = comms.messaging.send(
        "alice", "bob", "Claim a", claims=[ExistingFileClaim(Path("a.py"))]
    ).message
    original_bytes = comms.bus.log.path.read_bytes()
    with comms.registry.store.editing() as edit:
        threads = edit.document.threads
        threads["bob"] = replace(threads["bob"], created_at=threads["alice"].created_at)
        edit.commit()
    comms = Comms(comms.root)
    with pytest.raises(RelationViolationError, match=r"^Registry creation identities collide\.$"):
        comms.messaging.send(
            "bob", "alice", "Cannot release under duplicate ID", releases=["a.py"]
        ).message
    assert comms.bus.log.path.read_bytes() == original_bytes
    assert read_history(comms) == [claimed]


@pytest.mark.parametrize("suffix", ["//a.py", "/./a.py"])
def test_owner_release_alias_rejected_before_it_can_relinquish_claim(
    tmp_path: Path, suffix: str
) -> None:
    comms, worktree = _participants(tmp_path)
    claimed = comms.messaging.send(
        "alice", "bob", "Claim canonical", claims=[ExistingFileClaim(Path("a.py"))]
    ).message
    canonical = str(worktree / "a.py")
    raw_alias = str(worktree) + suffix
    assert os.path.samefile(canonical, raw_alias)
    with pytest.raises(RelationViolationError, match="canonical spelling"):
        comms.messaging.send("alice", "bob", "Alias release denied", releases=[raw_alias]).message
    assert read_history(comms) == [claimed]
    assert comms.bus.log.claim_projection()[canonical].owner == "alice"
    released = comms.messaging.send("alice", "bob", "Exact release", releases=[canonical]).message
    assert released.claim_transition is not None
    assert comms.bus.log.claim_projection().get(canonical) is None


def test_owner_can_release_exact_claim_after_file_deletion(tmp_path: Path) -> None:
    comms, worktree = _participants(tmp_path)
    comms.messaging.send(
        "alice", "bob", "Claim before deletion", claims=[ExistingFileClaim(Path("a.py"))]
    ).message
    (worktree / "a.py").unlink()
    comms.messaging.send("alice", "bob", "Release deleted file", releases=["a.py"]).message
    assert comms.bus.log.claim_projection().get(str(worktree / "a.py")) is None
    assert len(read_history(comms)) == 2


def test_concurrent_whole_set_exactly_one_winner(tmp_path: Path) -> None:
    comms, _ = _participants(tmp_path)
    simultaneous = Barrier(2)

    def attempt(name: str) -> str:
        owner = Comms(comms.root)
        simultaneous.wait(timeout=3)
        try:
            owner.messaging.send(
                name,
                "observer",
                "Competing claims",
                claims=[ExistingFileClaim(Path("a.py")), ExistingFileClaim(Path("b.py"))],
            ).message
        except ClaimConflict:
            return "lost"
        return "won"

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = tuple(pool.map(attempt, ("alice", "bob")))
    assert sorted(results) == ["lost", "won"]
    assert len(read_history(comms)) == 1


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
        bus = comms.bus.log.path
        trigger = (
            target == str(bus) and bus.stat().st_size > 0
            if fail_at == "bus"
            else (target == str(bus.parent) and bus.exists() and bus.stat().st_size > 0)
        )
        if trigger and not failed:
            failed = True
            raise OSError("injected claim-row fsync failure")
        real_fsync(descriptor)

    monkeypatch.setattr(store_files.os, "fsync", fsync)
    with pytest.raises(ClaimEnvelopeUnknownError, match="UNKNOWN"):
        comms.messaging.send(
            "alice", "bob", "Maybe claimed", claims=[ExistingFileClaim(Path("a.py"))]
        ).message
    assert failed
    # No retry of Alice's uncertain send; a separate guarded reader resyncs
    # the complete visible row before either observing it or rejecting Bob.
    messages = read_history(comms)
    assert len(messages) == 1
    assert messages[0].claim_transition is not None
    with pytest.raises(ClaimConflict):
        comms.messaging.send(
            "bob", "alice", "Conflict", claims=[ExistingFileClaim(Path("a.py"))]
        ).message
    assert read_history(comms) == messages


def test_large_transition_reopens_and_exports_without_a_second_parser_limit(
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
    committed = comms.messaging.send(
        "alice", "bob", "Large claim",
        claims=[ExistingFileClaim(Path(path)) for path in resources],
    ).message
    assert len(json.dumps(committed.to_wire()["claim_transition"])) > 16_384
    assert len(Comms(comms.root).bus.log.claim_projection()) == len(resources)
    assert read_history(comms) == [committed]
    assert comms.bus.log.messages_for_references((committed.reference,)) == (committed,)
    assert all(owner.owner == "alice" for owner in comms.bus.log.claim_projection().values())
    destination = tmp_path / "safe-export.jsonl"
    acknowledgement = comms.views.export_wire(
        destination,
        format=JsonlFormat(),
        scope=EverythingScope(),
        limit=FullLimit(),
    )
    exported = [json.loads(line) for line in destination.read_text().splitlines()]
    assert acknowledgement.exported_messages == 1
    assert len(exported) == 2 and exported[1]["record"] == "message"
    assert exported[1]["message"] == committed.to_wire()


def test_invalid_claim_record_cannot_be_exposed_as_plain_message(tmp_path: Path) -> None:
    comms, _ = _participants(tmp_path)
    public = Message("alice", "bob", "attempt", InfoKind, seq=1).to_wire()
    public["claim_transition"] = {"claims": ["/tmp/a.py"]}
    comms.bus.log.path.write_bytes(json.dumps(public).encode() + b"\n")
    with pytest.raises(StoredHistoryUnreadable, match="Malformed public bus row") as malformed:
        read_history(comms)
    assert type(malformed.value.__cause__) is MalformedRecord
    assert "claim_transition" in str(malformed.value.__cause__)


def _private_verified_rows(comms: Comms) -> list[tuple[object, object, object]]:
    with store_files._store_lock(comms.bus.log.path):
        metadata = comms.bus.log.private_marker_unlocked()
        return list(comms.bus.log.verified_records_unlocked(metadata))


def _ordinary_locked_iterator(comms: Comms) -> list[Message]:
    with comms.bus.log.full_history_snapshot() as (_, messages):
        return list(messages)


def _ordinary_snapshot(comms: Comms) -> list[object]:
    with comms.bus.log._record_snapshot() as (_, records):
        return list(records)


def test_claim_bearing_visible_failed_fsync_all_reader_routes_block_then_resync(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    comms, worktree = _participants(tmp_path)
    bus = comms.bus.log.path
    real_fsync = os.fsync
    phase = "writer"
    fsyncs = 0

    def fail_fsync(descriptor: int) -> None:
        nonlocal fsyncs
        target = os.readlink(f"/proc/self/fd/{descriptor}")
        if target == str(bus) and bus.stat().st_size > 0:
            fsyncs += 1
            if phase != "allow":
                raise OSError("claim row visible but not durable")
        real_fsync(descriptor)

    monkeypatch.setattr(store_files.os, "fsync", fail_fsync)
    with pytest.raises(ClaimEnvelopeUnknownError, match="UNKNOWN"):
        comms.messaging.send(
            "alice", "bob", "Uncertain claimed envelope", claims=[ExistingFileClaim(Path("a.py"))]
        ).message
    assert bus.read_bytes().endswith(b"\n")
    raw = json.loads(bus.read_text().splitlines()[0])
    assert raw["claim_transition"]["claims"] == [str(worktree / "a.py")]
    reference = MessageReference(raw["seq"], raw["id"])
    phase = "reader_blocked"
    denied = (
        lambda: comms.bus.log.messages_for_references((reference,)),
        lambda: _ordinary_locked_iterator(comms),
        lambda: _ordinary_snapshot(comms),
        lambda: read_history(comms),
        lambda: read_history(comms),
        lambda: comms.bus.log.latest_sequence(),
        lambda: comms.bus.log.claim_projection(),
        lambda: _private_verified_rows(comms),
    )
    for read in denied:
        with pytest.raises(RelationViolationError, match="UNKNOWN"):
            read()
    assert fsyncs == 1 + len(denied)
    phase = "allow"
    message, = comms.bus.log.messages_for_references((reference,))
    assert message is not None and message.claim_transition is not None
    assert _ordinary_locked_iterator(comms) == [message]
    assert _ordinary_snapshot(comms)[0][0] == message
    assert read_history(comms) == [message]
    assert len(read_history(comms)) == 1
    assert comms.bus.log.latest_sequence() == message.seq
    assert comms.bus.log.claim_projection()[str(worktree / "a.py")].owner == "alice"
    assert len(_private_verified_rows(comms)) == 1


def test_claim_bearing_json_valid_no_newline_never_exposed(tmp_path: Path) -> None:
    comms, _ = _participants(tmp_path)
    committed = comms.messaging.send(
        "alice", "bob", "Original claim", claims=[ExistingFileClaim(Path("a.py"))]
    ).message
    bus = comms.bus.log.path
    assert bus.read_bytes().endswith(b"\n")
    bus.write_bytes(bus.read_bytes()[:-1])  # crash left an incomplete but JSON-valid tail
    denied = (
        lambda: comms.bus.log.messages_for_references((committed.reference,)),
        lambda: _ordinary_locked_iterator(comms),
        lambda: _ordinary_snapshot(comms),
        lambda: read_history(comms),
        lambda: read_history(comms),
        lambda: comms.bus.log.latest_sequence(),
        lambda: comms.bus.log.claim_projection(),
        lambda: _private_verified_rows(comms),
    )
    for read in denied:
        with pytest.raises(
            RelationViolationError, match="Private bus checkpoint root/size changed"
        ):
            read()


def test_reserved_metadata_sequence_is_not_a_committed_message(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    comms, _ = _participants(tmp_path)
    bus = comms.bus.log.path
    real_open = os.open

    def fail_before_append(
        path: str | os.PathLike[str], flags: int, *args: object, **kwargs: object
    ) -> int:
        if str(path) == str(bus) and flags & os.O_APPEND:
            raise OSError("injected failure after metadata reservation")
        return real_open(path, flags, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(store_files.os, "open", fail_before_append)
        with pytest.raises(ClaimEnvelopeUnknownError, match="UNKNOWN"):
            comms.messaging.send(
                "alice", "bob", "Never appended", claims=[ExistingFileClaim(Path("a.py"))]
            ).message
    assert json.loads((comms.root / "bus_meta.json").read_text())["last_seq"] == 1
    assert read_history(comms) == []
    assert comms.bus.log.latest_sequence() == 0
    with comms.bus.log.full_history_snapshot() as (through, rows):
        assert through == 0 and list(rows) == []
    committed = comms.messaging.send(
        "bob", "alice", "Next safe claim", claims=[ExistingFileClaim(Path("a.py"))]
    ).message
    assert committed.seq == 2  # reserved gaps are never reused
    assert comms.bus.log.latest_sequence() == 2


@pytest.mark.parametrize("phase", ["before_bus_append", "after_bus_append_before_fsync"])
def test_sigkill_across_claim_send_recovers_one_row_or_none(tmp_path: Path, phase: str) -> None:
    comms, _ = _participants(tmp_path)
    code = """
import os, signal, sys
from pathlib import Path
sys.path.insert(0, sys.argv[2])
import agent_comms.store_files as store_files
from agent_comms.comms import Comms
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
from agent_comms.envelope_claim_transitions import ExistingFileClaim
Comms(root).messaging.send(
    'alice', 'bob', 'Interrupted claim', claims=[ExistingFileClaim(Path('a.py'))]
)
"""
    source = str(Path(__file__).resolve().parents[1] / "src")
    env = os.environ.copy()
    for key in (
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
    rows = read_history(comms)  # guarded reader fsyncs a complete visible row
    if phase == "before_bus_append":
        assert rows == []
        comms.messaging.send(
            "bob", "alice", "First durable claim", claims=[ExistingFileClaim(Path("a.py"))]
        ).message
    else:
        assert len(rows) == 1 and rows[0].claim_transition is not None
        with pytest.raises(ClaimConflict):
            comms.messaging.send(
                "bob",
                "alice",
                "No replay or duplicate claim",
                claims=[ExistingFileClaim(Path("a.py"))],
            ).message
    assert len(read_history(comms)) == 1
