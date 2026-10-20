"""Original transitions validate live holders; committed projection replays without them."""
import os
from dataclasses import replace
from pathlib import Path

import pytest

from agent_comms.child_process import ProcessIdentity
from agent_comms.envelope_claim_transitions import (
    ClaimConflict, ClaimOwner, ClaimProjection, ClaimRelease, ClaimTransition,
    ClaimTransitionError, ExistingFileClaim, NativeClaim, ReleasedClaims, WakeClaim, WakeClaims,
)
from agent_comms.field_codec import FieldCodec, MalformedRecord
from agent_comms.messages import ClaimMessage, ClaimNoticeKind, Message

G1 = "a" * 32
G2 = "b" * 32


def transition(seq, *, owner="owner", incarnation="1.0", claims=(), releases=(), generation=None):
    identity = dict(owner=owner, incarnation=incarnation, seq=seq, message_id=f"msg-{seq}")
    if releases:
        if claims or generation is not None:
            raise ClaimTransitionError("Acquisition and release are separate originals")
        return ReleasedClaims(**identity, releases=tuple(releases))
    return WakeClaims(**identity, claims=tuple(claims), generation=generation,
                      claim_scope=NativeClaim(owner_claimed_generation=1,
                                              operation_id=f"{seq:032x}",
                                              turn_id=f"turn-{seq}", input_id=f"input-{seq}"))


def resources(tmp_path):
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    (worktree / "a.py").write_text("a")
    (worktree / "b.py").write_text("b")
    return (worktree, ExistingFileClaim(Path("a.py")).normalized(worktree),
            ExistingFileClaim(Path("b.py")).normalized(worktree))


def live_registry(comms, *, name="owner", birth=1.0):
    thread = comms.registry.new_thread(name, frozenset(), str(comms.root), created_at=birth)
    comms.registry.declare(replace(thread, process_identity=ProcessIdentity.capture(os.getpid())))
    return comms.registry.snapshot()


def test_same_name_can_take_a_previous_birth_only_with_current_registry_evidence(comms, tmp_path):
    _, resource, _ = resources(tmp_path)
    held = transition(1, claims=(resource,), generation=G1).project(ClaimProjection())
    registry = live_registry(comms)
    successor = transition(2, incarnation="2.0", claims=(resource,), generation=G2)
    with pytest.raises(ClaimConflict):
        successor.apply(held, registry)
    # The registry now proves the original birth is no longer a live holder.
    replacement = live_registry(comms, birth=2.0)
    saved = FieldCodec.decode(ClaimTransition, FieldCodec.encode(successor))
    moved = saved.apply(held, replacement)
    assert moved[resource].incarnation == "2.0" and moved[resource].generation == G2
    assert saved.project(held) == moved  # replay never consults today's registry
    with pytest.raises(ClaimTransitionError, match="exact owner"):
        transition(3, releases=(ClaimRelease(resource, G1),)).project(moved)


def test_selected_wake_binding_survives_claim_projection(tmp_path):
    _, resource, _ = resources(tmp_path)
    scope = WakeClaim(wire_root_id="c" * 32, source_seq=7, source_message_id="source-7",
                      wake_assignment_id="cohort-v1:" + "d" * 64, wake_revision=3,
                      recipient_lookup="e" * 32, execution_id="execution-7", operation_id="f" * 32,
                      owner_claimed_generation=2, turn_id="turn-7", participant_generation=1,
                      attempt_ordinal=1)
    claimed = WakeClaims("owner", "1.0", 8, "msg-8", G1, scope, (resource,))
    raw = FieldCodec.encode(claimed)
    decoded = FieldCodec.decode(ClaimTransition, raw)
    assert decoded == claimed
    assert decoded.project(ClaimProjection())[resource].claim_scope == scope
    message = Message("owner", "peer", "Claim file", ClaimNoticeKind, seq=8, timestamp=1.0)
    bound = ClaimMessage.of(message, replace(claimed, message_id=message.message_id))
    assert Message.from_wire(bound.to_wire()) == bound
    key = FieldCodec.record_key(WakeClaims, "claim_scope")
    invalid = {**raw[key], "version": 99}
    with pytest.raises(ValueError):
        FieldCodec.decode(ClaimTransition, {**raw, key: invalid})
    with pytest.raises(ValueError, match="Wake claim_scope version or revision"):
        FieldCodec.decode(WakeClaim, invalid)
    with pytest.raises(ValueError):
        FieldCodec.decode(ClaimTransition, {**raw, key: None})
    with pytest.raises(ValueError, match="Null field"):
        FieldCodec.decode(WakeClaims, {**raw, key: None})


def test_existing_physical_worktree_files_and_aliases(tmp_path):
    worktree, a, _ = resources(tmp_path)
    assert a == ExistingFileClaim(Path(worktree / "a.py")).normalized(worktree)
    assert a == ExistingFileClaim(Path("./a.py")).normalized(worktree)
    other = tmp_path / "other"
    other.mkdir()
    (other / "a.py").write_text("a")
    assert ExistingFileClaim(other / "a.py").normalized(worktree) == str(other / "a.py")
    for path in ("missing.py", "../outside.py", "."):
        with pytest.raises(ClaimTransitionError):
            ExistingFileClaim(Path(path)).normalized(worktree)
    (worktree / "link.py").symlink_to(worktree / "a.py")
    (worktree / "dir").mkdir()
    (worktree / "dir" / "sub.py").symlink_to(worktree / "a.py")
    for path in ("link.py", "dir/sub.py"):
        with pytest.raises(ClaimTransitionError, match="Symlink"):
            ExistingFileClaim(Path(path)).normalized(worktree)
    (worktree / "hard.py").hardlink_to(worktree / "b.py")
    with pytest.raises(ClaimTransitionError, match="singly-linked"):
        ExistingFileClaim(Path("b.py")).normalized(worktree)
    (tmp_path / "root-link").symlink_to(worktree, target_is_directory=True)
    with pytest.raises(ClaimTransitionError, match="physical"):
        ExistingFileClaim(Path("a.py")).normalized(tmp_path / "root-link")


def test_two_serialized_conflicting_entrants_have_one_winner_and_one_synchronous_loser(comms, tmp_path):
    _, a, _ = resources(tmp_path)
    registry = live_registry(comms)
    initial = ClaimProjection()
    first = transition(1, claims=(a,), generation=G1).apply(initial, registry)
    with pytest.raises(ClaimConflict) as conflict:
        transition(2, owner="competitor", claims=(a,), generation=G2).apply(first, registry)
    assert conflict.value.existing.owner == "owner" and conflict.value.existing.seq == 1
    assert dict(initial) == {} and list(first) == [a] and first[a].message_id == "msg-1"
    with pytest.raises(TypeError):
        first[a] = None


def test_same_holder_moves_scope_but_another_holder_cannot(comms, tmp_path):
    _, resource, _ = resources(tmp_path)
    registry = live_registry(comms)
    held = transition(1, claims=(resource,), generation=G1).apply(ClaimProjection(), registry)
    moved = transition(2, claims=(resource,), generation=G2).apply(held, registry)
    assert held[resource].generation == G1 and moved[resource].generation == G2
    for changed in ({"owner": "other"}, {"incarnation": "2.0"}):
        with pytest.raises(ClaimConflict):
            transition(3, claims=(resource,), generation=G1, **changed).apply(moved, registry)
    with pytest.raises(ClaimTransitionError, match="exact owner"):
        transition(3, releases=(ClaimRelease(resource, G1),)).project(moved)
    assert dict(transition(3, releases=(ClaimRelease(resource, G2),)).project(moved)) == {}


def test_full_set_claim_and_release_are_all_or_nothing(comms, tmp_path):
    _, a, b = resources(tmp_path)
    registry = live_registry(comms)
    first = transition(1, claims=(b,), generation=G1).apply(ClaimProjection(), registry)
    with pytest.raises(ClaimConflict) as conflict:
        transition(2, owner="peer", claims=(a, b), generation=G2).apply(first, registry)
    assert conflict.value.existing.resource == b and dict(first) == {b: first[b]}
    with pytest.raises(ClaimTransitionError, match="live claim"):
        transition(3, releases=(ClaimRelease(a, G1),)).project(first)
    assert dict(first) == {b: first[b]}
    with pytest.raises(ClaimTransitionError, match="Duplicate"):
        transition(2, claims=(a, a), generation=G2)
    with pytest.raises(ClaimTransitionError, match="separate"):
        transition(2, claims=(b,), releases=(ClaimRelease(b, G1),), generation=G2)


def test_exact_incarnation_and_generation_fence_releases(tmp_path):
    _, a, _ = resources(tmp_path)
    held = transition(1, claims=(a,), generation=G1).project(ClaimProjection())
    for attempt in (transition(2, owner="peer", incarnation="2.0", releases=(ClaimRelease(a, G1),)),
                    transition(2, incarnation="3.0", releases=(ClaimRelease(a, G1),)),
                    transition(2, releases=(ClaimRelease(a, G2),))):
        with pytest.raises(ClaimTransitionError, match="exact owner"):
            attempt.project(held)
        assert held[a].generation == G1
    released = transition(2, releases=(ClaimRelease(a, G1),)).project(held)
    assert dict(released) == {} and released.last_seq == 2
    with pytest.raises(ClaimTransitionError, match="increase"):
        transition(1, owner="peer", claims=(a,), generation=G2).project(released)
    new = transition(3, owner="peer", claims=(a,), generation=G2).project(released)
    assert new[a].owner == "peer" and new[a].seq == 3
    with pytest.raises(ClaimTransitionError, match="malformed"):
        ClaimProjection(0, {a: replace(new[a], seq=3)})
    with pytest.raises(MalformedRecord, match="JSON object"):
        ClaimProjection(1, [])
    with pytest.raises(ClaimTransitionError, match="exact owner"):
        transition(4, releases=(ClaimRelease(a, G1),)).project(new)


def test_renamed_owner_releases_by_incarnation_without_reusing_stale_generation(tmp_path):
    _, a, _ = resources(tmp_path)
    held = transition(1, owner="alice", claims=(a,), generation=G1).project(ClaimProjection())
    release = transition(2, owner="alice-new", releases=(ClaimRelease(a, G1),))
    released = release.project(held)
    assert dict(released) == {} and released.last_seq == 2 and held[a].owner == "alice"
    assert release.project(held) == released
    new = transition(3, owner="bob", incarnation="2.0", claims=(a,), generation=G2).project(released)
    for stale in (transition(4, owner="alice-new", releases=(ClaimRelease(a, G1),)),
                  transition(4, owner="alice", incarnation="3.0", releases=(ClaimRelease(a, G2),)),
                  transition(4, owner="bob", incarnation="2.0", releases=(ClaimRelease(a, G1),))):
        with pytest.raises(ClaimTransitionError, match="exact owner"):
            stale.project(new)
    assert new[a].owner == "bob" and new[a].generation == G2


def test_owned_application_is_deterministic_and_requires_order(tmp_path):
    _, a, b = resources(tmp_path)
    first = transition(1, claims=(a,), generation=G1).project(ClaimProjection())
    both = transition(7, claims=(b,), generation=G2).project(first)
    released = transition(8, releases=(ClaimRelease(a, G1),)).project(both)
    assert list(released) == [b] and released[b].seq == 7
    assert transition(8, releases=(ClaimRelease(a, G1),)).project(both) == released
    for stale in (transition(8, releases=(ClaimRelease(b, G2),)), transition(1, claims=(a,), generation=G1)):
        with pytest.raises(ClaimTransitionError, match="increase"):
            stale.project(released)


def test_only_exact_typed_nonempty_sequenced_envelopes(tmp_path):
    _, a, _ = resources(tmp_path)
    for seq, generation in ((True, G1), (0, G1), (1, None), (1, "f" * 31), (1, G1.upper())):
        with pytest.raises(MalformedRecord):
            transition(seq, claims=(a,), generation=generation)
    with pytest.raises(MalformedRecord):
        replace(transition(1, claims=(a,), generation=G1), claims=[a])
    with pytest.raises(MalformedRecord):
        ReleasedClaims("owner", "1.0", 1, "m", ("bad",))
    with pytest.raises(ClaimTransitionError):
        transition(1, generation=G1)
    with pytest.raises(ClaimTransitionError):
        transition(1, releases=(ClaimRelease(a, G1),), generation=G2)


def test_path_aliases_are_rejected_as_duplicate_normalized_resources(tmp_path):
    worktree, a, _ = resources(tmp_path)
    duplicate = (ExistingFileClaim(Path("a.py")).normalized(worktree),
                 ExistingFileClaim(Path(worktree / "a.py")).normalized(worktree))
    assert duplicate == (a, a)
    with pytest.raises(ClaimTransitionError, match="Duplicate"):
        transition(1, claims=duplicate, generation=G1)


def test_raw_path_aliases_cannot_acquire_or_release_same_file(comms, tmp_path):
    worktree, a, _ = resources(tmp_path)
    registry = live_registry(comms)
    held = transition(1, claims=(a,), generation=G1).apply(ClaimProjection(), registry)
    for alias in (f"{worktree}//a.py", f"{worktree}/./a.py"):
        with pytest.raises(MalformedRecord, match="canonical absolute"):
            transition(2, owner="competitor", claims=(alias,), generation=G2)
        with pytest.raises(MalformedRecord, match="canonical absolute"):
            ClaimRelease(alias, G1)
        claiming = FieldCodec.encode(transition(2, claims=(a,), generation=G2))
        claiming[FieldCodec.record_key(WakeClaims, "claims")] = [alias]
        with pytest.raises(MalformedRecord, match="canonical absolute"):
            FieldCodec.decode(ClaimTransition, claiming)
        releasing = FieldCodec.encode(transition(2, releases=(ClaimRelease(a, G1),)))
        releasing["releases"][0]["resource"] = alias
        with pytest.raises(MalformedRecord):
            FieldCodec.decode(ClaimTransition, releasing)
        with pytest.raises(MalformedRecord, match="canonical absolute"):
            FieldCodec.decode(ReleasedClaims, releasing)
        assert list(held) == [a] and held[a].owner == "owner" and held.last_seq == 1
    with pytest.raises(ClaimConflict) as loser:
        transition(2, owner="competitor", claims=(a,), generation=G2).apply(held, registry)
    assert loser.value.existing.resource == a
    assert ExistingFileClaim(Path(f"{worktree}//a.py")).normalized(worktree) == a


def test_stored_claims_decode_without_resolving_changed_filesystem(tmp_path, monkeypatch):
    _, a, b = resources(tmp_path)
    claimed = transition(1, claims=(a,), generation=G1)
    records = (claimed, ClaimRelease(a, G1), claimed.claimed(a))
    encoded = tuple(FieldCodec.encode(record) for record in records)
    Path(a).unlink()
    Path(a).symlink_to(b)
    def forbidden_resolve(*args, **kwargs):
        raise AssertionError("Stored resource decoding must not resolve current files")
    with monkeypatch.context() as patch:
        patch.setattr(Path, "resolve", forbidden_resolve)
        for record, raw in zip(records, encoded, strict=True):
            assert FieldCodec.decode(type(record), raw) == record


def test_no_claim_state_or_sidecar_is_written_by_pure_application(tmp_path):
    worktree, a, _ = resources(tmp_path)
    before = set(worktree.iterdir())
    assert transition(1, claims=(a,), generation=G1).project(ClaimProjection())[a].owner == "owner"
    assert set(worktree.iterdir()) == before and os.access(worktree / "a.py", os.R_OK)
