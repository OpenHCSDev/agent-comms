"""ClaimTransition owns atomic application; durability belongs to its checkpoint."""

import os
from dataclasses import replace
from pathlib import Path

import pytest

from agent_comms.envelope_claim_transitions import (
    ClaimConflict, ClaimOwner, ClaimProjection, ClaimRelease, ClaimTransition,
    ClaimTransitionError, ExistingFileClaim, NativeClaim, ReleasedClaims,
    UnscopedClaims, WakeClaim, WakeClaims,
)
from agent_comms.field_codec import FieldCodec, MalformedRecord
from agent_comms.messages import ClaimMessage, ClaimNoticeKind, Message

G1 = "a" * 32
G2 = "b" * 32


def test_same_name_can_take_a_previous_birth_only_with_current_registry_evidence(comms, tmp_path):
    from test_todos import thread

    _, resource, _ = resources(tmp_path)
    from agent_comms.child_process import ProcessIdentity
    from agent_comms.thread_status import RunningThreadStatus

    comms.registry.declare(replace(thread("owner", comms.root, 2.0),
                                   process_identity=ProcessIdentity.capture(os.getpid())), RunningThreadStatus())
    comms.registry.declare(replace(thread("foreign", comms.root, 3.0),
                                   process_identity=ProcessIdentity.capture(os.getpid())), RunningThreadStatus())
    registry = comms.registry.snapshot()
    scope = NativeClaim(owner_claimed_generation=1, operation_id="c" * 32,
                        turn_id="turn-owner", input_id="input-owner")
    held = WakeClaims("owner", "1.0", 1, "msg-1", claims=(resource,),
                      generation=G1, claim_scope=scope).project(ClaimProjection())
    original = held[resource]
    assert not original.live(registry)
    original.require_holder("owner", "1.0")
    for owner, incarnation in (("foreign", "3.0"), ("owner", "4.0")):
        with pytest.raises(ClaimConflict):
            original.require_holder(owner, incarnation)
    successor = WakeClaims("owner", "2.0", 2, "msg-2", claims=(resource,),
                           generation=G2, claim_scope=scope)
    saved = FieldCodec.decode(ClaimTransition, FieldCodec.encode(successor))
    moved = saved.apply(held, registry)
    assert moved[resource].incarnation == "2.0" and moved[resource].generation == G2
    assert moved[resource].live(registry)
    comms.registry.declare(replace(thread("owner", comms.root, 4.0),
                                   process_identity=ProcessIdentity.capture(os.getpid())), RunningThreadStatus())
    assert saved.project(ClaimProjection(held.last_seq)) == moved  # replay is pure
    with pytest.raises(ClaimConflict):
        saved.require_holders(held)
    with pytest.raises(ClaimTransitionError, match="exact owner"):
        transition(3, incarnation="1.0", releases=(ClaimRelease(resource, G1),)).project(moved)


def test_selected_wake_binding_survives_claim_projection(tmp_path):
    _, resource, _ = resources(tmp_path)
    claim_scope = WakeClaim(
        wire_root_id="c" * 32, source_seq=7, source_message_id="source-7",
        wake_assignment_id="cohort-v1:" + "d" * 64, wake_revision=3,
        recipient_lookup="e" * 32, execution_id="execution-7", operation_id="f" * 32,
        owner_claimed_generation=2, turn_id="turn-7", participant_generation=1,
        attempt_ordinal=1,
    )
    claimed = WakeClaims("owner", "epoch-1", 8, "msg-8", claims=(resource,),
                         generation=G1, claim_scope=claim_scope)
    raw = FieldCodec.encode(claimed)
    decoded = FieldCodec.decode(ClaimTransition, raw)
    assert decoded == claimed
    assert decoded.project(ClaimProjection())[resource].claim_scope == claim_scope
    message = Message("owner", "owner", "Claim file", ClaimNoticeKind, seq=8, timestamp=1.0)
    bound = ClaimMessage.of(message, replace(claimed, message_id=message.message_id))
    assert Message.from_wire(bound.to_wire()) == bound
    admission_key = FieldCodec.record_key(WakeClaims, "claim_scope")
    invalid = {**raw, admission_key: {**raw[admission_key], "version": 99}}
    with pytest.raises(ValueError, match="version"):
        FieldCodec.decode(ClaimTransition, invalid)
    null_claim = raw.copy()
    null_claim[admission_key] = None
    with pytest.raises(ValueError, match="received NoneType"):
        FieldCodec.decode(ClaimTransition, null_claim)


def transition(seq, *, owner="owner", incarnation="epoch-1", claims=(), releases=(), generation=None):
    if claims:
        return UnscopedClaims(owner, incarnation, seq, f"msg-{seq}",
                              claims=tuple(claims), generation=generation, releases=tuple(releases))
    if generation is not None:
        raise ClaimTransitionError("Releases cannot mint an acquisition generation.")
    return ReleasedClaims(owner, incarnation, seq, f"msg-{seq}", releases=tuple(releases))


def resources(tmp_path):
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    (worktree / "a.py").write_text("a")
    (worktree / "b.py").write_text("b")
    return (worktree, ExistingFileClaim(Path("a.py")).normalized(worktree),
            ExistingFileClaim(Path("b.py")).normalized(worktree))


def test_existing_physical_worktree_files_and_aliases(tmp_path):
    worktree, a, _ = resources(tmp_path)
    assert a == ExistingFileClaim(Path(worktree / "a.py")).normalized(worktree)
    assert a == ExistingFileClaim(Path("./a.py")).normalized(worktree)
    other = tmp_path / "other"
    other.mkdir()
    (other / "a.py").write_text("a")
    outside = ExistingFileClaim(other / "a.py").normalized(worktree)
    assert outside == str(other / "a.py") and outside != a
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


def test_two_serialized_conflicting_entrants_have_one_winner_and_one_synchronous_loser(tmp_path):
    _, a, _ = resources(tmp_path)
    initial = ClaimProjection()
    first = transition(1, claims=(a,), generation=G1).project(initial)
    with pytest.raises(ClaimConflict) as conflict:
        transition(2, owner="competitor", claims=(a,), generation=G2).require_holders(first)
    assert conflict.value.existing.owner == "owner" and conflict.value.existing.seq == 1
    assert dict(initial) == {} and list(first) == [a] and first[a].message_id == "msg-1"
    with pytest.raises(TypeError):
        first[a] = None


def test_same_holder_moves_scope_but_another_holder_cannot(tmp_path):
    _, resource, _ = resources(tmp_path)
    held = transition(1, claims=(resource,), generation=G1).project(ClaimProjection())
    moved = transition(2, claims=(resource,), generation=G2).project(held)
    assert held[resource].generation == G1
    assert moved[resource].generation == G2
    for changed in ({"owner": "other"}, {"incarnation": "new-birth"}):
        with pytest.raises(ClaimConflict):
            transition(3, claims=(resource,), generation=G1, **changed).require_holders(moved)
    with pytest.raises(ClaimTransitionError, match="exact owner"):
        transition(3, releases=(ClaimRelease(resource, G1),)).project(moved)
    assert dict(transition(3, releases=(ClaimRelease(resource, G2),)).project(moved)) == {}


def test_full_set_claim_and_release_are_all_or_nothing(tmp_path):
    _, a, b = resources(tmp_path)
    first = transition(1, claims=(b,), generation=G1).project(ClaimProjection())
    with pytest.raises(ClaimConflict) as conflict:
        transition(2, owner="peer", claims=(a, b), generation=G2).require_holders(first)
    assert conflict.value.existing.resource == b and dict(first) == {b: first[b]}
    with pytest.raises(ClaimTransitionError, match="live claim"):
        transition(3, releases=(ClaimRelease(a, G1),)).project(first)
    assert dict(first) == {b: first[b]}
    with pytest.raises(ClaimTransitionError, match="Duplicate"):
        transition(2, claims=(a, a), generation=G2)
    with pytest.raises(ClaimTransitionError, match="releases nothing"):
        transition(2, claims=(b,), releases=(ClaimRelease(b, G1),), generation=G2)


def test_exact_incarnation_and_generation_fence_releases(tmp_path):
    _, a, _ = resources(tmp_path)
    held = transition(1, claims=(a,), generation=G1).project(ClaimProjection())
    for attempt in (
        transition(2, owner="peer", incarnation="epoch-2", releases=(ClaimRelease(a, G1),)),
        transition(2, incarnation="old-epoch", releases=(ClaimRelease(a, G1),)),
        transition(2, releases=(ClaimRelease(a, G2),)),
    ):
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
        ClaimProjection(0, {a: new[a]})
    with pytest.raises(MalformedRecord, match="JSON object"):
        ClaimProjection(1, [])
    with pytest.raises(ClaimTransitionError, match="exact owner"):
        transition(4, releases=(ClaimRelease(a, G1),)).project(new)


def test_renamed_owner_releases_by_incarnation_without_reusing_stale_generation(tmp_path):
    _, a, _ = resources(tmp_path)
    held = transition(1, owner="alice", incarnation="birth-1", claims=(a,), generation=G1).project(ClaimProjection())
    renamed_release = transition(2, owner="alice-new", incarnation="birth-1", releases=(ClaimRelease(a, G1),))
    released = renamed_release.project(held)
    assert dict(released) == {} and released.last_seq == 2 and held[a].owner == "alice"
    assert renamed_release.project(held) == released
    reacquired = transition(3, owner="bob", incarnation="birth-2", claims=(a,), generation=G2).project(released)
    for stale in (
        transition(4, owner="alice-new", incarnation="birth-1", releases=(ClaimRelease(a, G1),)),
        transition(4, owner="alice", incarnation="birth-3", releases=(ClaimRelease(a, G2),)),
        transition(4, owner="bob", incarnation="birth-2", releases=(ClaimRelease(a, G1),)),
    ):
        with pytest.raises(ClaimTransitionError, match="exact owner"):
            stale.project(reacquired)
    assert reacquired[a].owner == "bob" and reacquired[a].generation == G2


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
    mistyped = (
        lambda: transition(True, claims=(a,), generation=G1),
        lambda: transition(0, claims=(a,), generation=G1),
        lambda: transition(1, claims=(a,), generation=None),
        lambda: transition(1, claims=(a,), generation="f" * 31),
        lambda: transition(1, claims=(a,), generation=G1.upper()),
        lambda: ReleasedClaims("owner", "epoch", 1, "m", releases=("bad",)),
        lambda: UnscopedClaims("owner", "epoch", 1, "m", claims=[a], generation=G1),
    )
    for candidate in mistyped:
        with pytest.raises(MalformedRecord):
            candidate()
    for candidate in (
        lambda: transition(1),
        lambda: transition(1, releases=(ClaimRelease(a, G1),), generation=G2),
    ):
        with pytest.raises(ClaimTransitionError):
            candidate()


def test_path_aliases_are_rejected_as_duplicate_normalized_resources(tmp_path):
    worktree, a, _ = resources(tmp_path)
    duplicate = (ExistingFileClaim(Path("a.py")).normalized(worktree),
                 ExistingFileClaim(Path(worktree / "a.py")).normalized(worktree))
    assert duplicate == (a, a)
    with pytest.raises(ClaimTransitionError, match="Duplicate"):
        transition(1, claims=duplicate, generation=G1)


def test_raw_path_aliases_cannot_acquire_or_release_same_file(tmp_path):
    worktree, a, _ = resources(tmp_path)
    held = transition(1, claims=(a,), generation=G1).project(ClaimProjection())
    aliases = (f"{worktree}//a.py", f"{worktree}/./a.py")
    for alias in aliases:
        assert alias != a
        with pytest.raises(MalformedRecord, match="canonical absolute"):
            transition(2, owner="competitor", claims=(alias,), generation=G2)
        with pytest.raises(MalformedRecord, match="canonical absolute"):
            ClaimRelease(alias, G1)
        claiming_row = FieldCodec.encode(transition(2, owner="competitor", claims=(a,), generation=G2))
        claiming_row[FieldCodec.record_key(UnscopedClaims, "claims")] = [alias]
        with pytest.raises(MalformedRecord, match="canonical absolute"):
            FieldCodec.decode(ClaimTransition, claiming_row)
        releasing_row = FieldCodec.encode(transition(2, releases=(ClaimRelease(a, G1),)))
        releasing_row["releases"][0]["resource"] = alias
        with pytest.raises(MalformedRecord, match="canonical absolute"):
            FieldCodec.decode(ClaimTransition, releasing_row)
        assert list(held) == [a] and held[a].owner == "owner" and held.last_seq == 1
    with pytest.raises(ClaimConflict) as loser:
        transition(2, owner="competitor", claims=(a,), generation=G2).require_holders(held)
    assert loser.value.existing.resource == a
    assert ExistingFileClaim(Path(aliases[0])).normalized(worktree) == a


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
