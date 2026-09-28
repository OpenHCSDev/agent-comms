"""Pure claim projection; durable replay is tested at the bus boundary."""

import os
from dataclasses import replace
from pathlib import Path

import pytest

from agent_comms.envelope_claim_transitions import (
    ClaimConflict,
    ClaimOwner,
    ClaimProjection,
    ClaimRelease,
    ClaimTransition,
    ClaimTransitionError,
    ExistingFileClaim,
    WakeAdmission,
    apply_transition,
    project_verified_transitions,
)
from agent_comms.field_codec import FieldCodec
from agent_comms.messages import Message, MessageType

G1 = "a" * 32
G2 = "b" * 32


def test_selected_wake_binding_survives_claim_projection(tmp_path):
    _, resource, _ = resources(tmp_path)
    admission = WakeAdmission(
        wire_root_id="c" * 32,
        source_seq=7,
        source_message_id="source-7",
        wake_assignment_id="cohort-v1:" + "d" * 64,
        wake_revision=3,
        recipient_lookup="e" * 32,
        execution_id="execution-7",
        operation_id="f" * 32,
        owner_admission_generation=2,
        turn_id="turn-7",
        participant_generation=1,
        attempt_ordinal=1,
    )
    claimed = ClaimTransition("owner", "epoch-1", 8, "msg-8", (resource,), (), G1, admission)
    raw = FieldCodec.encode(claimed)
    decoded = FieldCodec.decode(ClaimTransition, raw)
    assert decoded == claimed
    assert apply_transition(ClaimProjection(), decoded)[resource].admission == admission

    message = Message("owner", "peer", "Claim file", MessageType.INFO, seq=8, timestamp=1.0)
    bound = replace(
        message,
        claim_transition=replace(claimed, message_id=message.message_id),
    )
    assert Message.from_wire(bound.to_wire()) == bound

    invalid = raw.copy()
    invalid["admission"]["version"] = 99
    with pytest.raises(ValueError, match="Wake admission version or revision"):
        FieldCodec.decode(ClaimTransition, invalid)
    null_admission = raw.copy()
    null_admission["admission"] = None
    with pytest.raises(ValueError, match="Null field"):
        FieldCodec.decode(ClaimTransition, null_admission)


def transition(
    seq, *, owner="owner", incarnation="epoch-1", claims=(), releases=(), generation=None
):
    return ClaimTransition(
        owner, incarnation, seq, f"msg-{seq}", tuple(claims), tuple(releases), generation
    )


def resources(tmp_path):
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    (worktree / "a.py").write_text("a")
    (worktree / "b.py").write_text("b")
    return (
        worktree,
        ExistingFileClaim(Path("a.py")).normalized(worktree),
        ExistingFileClaim(Path("b.py")).normalized(worktree),
    )


def test_existing_physical_worktree_files_and_aliases(tmp_path):
    worktree, a, _ = resources(tmp_path)
    assert a == ExistingFileClaim(Path(worktree / "a.py")).normalized(worktree)
    assert a == ExistingFileClaim(Path("./a.py")).normalized(worktree)
    other = tmp_path / "other"
    other.mkdir()
    (other / "a.py").write_text("a")
    assert a != ExistingFileClaim(Path("a.py")).normalized(other)
    for path in ("missing.py", "../outside.py", ".", other / "a.py"):
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
    initial = project_verified_transitions(())
    first = apply_transition(initial, transition(1, claims=(a,), generation=G1))
    with pytest.raises(ClaimConflict) as conflict:
        apply_transition(first, transition(2, owner="competitor", claims=(a,), generation=G2))
    assert conflict.value.existing.owner == "owner"
    assert conflict.value.existing.seq == 1
    assert dict(initial) == {}
    assert list(first) == [a]
    assert first[a].message_id == "msg-1"
    with pytest.raises(TypeError):
        first[a] = None


def test_full_set_claim_and_release_are_all_or_nothing(tmp_path):
    _, a, b = resources(tmp_path)
    first = apply_transition(ClaimProjection(), transition(1, claims=(b,), generation=G1))
    with pytest.raises(ClaimConflict) as conflict:
        apply_transition(first, transition(2, owner="peer", claims=(a, b), generation=G2))
    assert conflict.value.existing.resource == b
    assert dict(first) == {b: first[b]}
    with pytest.raises(ClaimTransitionError, match="live claim"):
        apply_transition(first, transition(3, releases=(ClaimRelease(a, G1),)))
    assert dict(first) == {b: first[b]}
    with pytest.raises(ClaimTransitionError, match="Duplicate"):
        transition(2, claims=(a, a), generation=G2)
    with pytest.raises(ClaimTransitionError, match="Duplicate"):
        transition(2, claims=(b,), releases=(ClaimRelease(b, G1),), generation=G2)


def test_exact_incarnation_and_generation_fence_releases(tmp_path):
    _, a, _ = resources(tmp_path)
    held = apply_transition(ClaimProjection(), transition(1, claims=(a,), generation=G1))
    for attempt in (
        # A distinct creation incarnation is denied; an authenticated rename
        # retains the same incarnation and is exercised separately below.
        transition(2, owner="peer", incarnation="epoch-2", releases=(ClaimRelease(a, G1),)),
        transition(2, incarnation="old-epoch", releases=(ClaimRelease(a, G1),)),
        transition(2, releases=(ClaimRelease(a, G2),)),
    ):
        with pytest.raises(ClaimTransitionError, match="exact owner"):
            apply_transition(held, attempt)
        assert held[a].generation == G1
    released = apply_transition(held, transition(2, releases=(ClaimRelease(a, G1),)))
    assert dict(released) == {} and released.last_seq == 2
    with pytest.raises(ClaimTransitionError, match="increase"):
        apply_transition(released, transition(1, owner="peer", claims=(a,), generation=G2))
    new = apply_transition(released, transition(3, owner="peer", claims=(a,), generation=G2))
    assert new[a].owner == "peer" and new[a].seq == 3
    with pytest.raises(ClaimTransitionError, match="malformed"):
        ClaimProjection(0, {a: ClaimOwner(a, "peer", "epoch", G2, 3, "msg-3")})
    with pytest.raises(ClaimTransitionError, match="malformed"):
        ClaimProjection(1, [])
    with pytest.raises(ClaimTransitionError, match="exact owner"):
        apply_transition(new, transition(4, releases=(ClaimRelease(a, G1),)))


def test_renamed_owner_releases_by_incarnation_without_reusing_stale_generation(tmp_path):
    _, a, _ = resources(tmp_path)
    held = apply_transition(
        ClaimProjection(),
        transition(1, owner="alice", incarnation="birth-1", claims=(a,), generation=G1),
    )
    renamed_release = transition(
        2, owner="alice-new", incarnation="birth-1", releases=(ClaimRelease(a, G1),)
    )
    released = apply_transition(held, renamed_release)
    assert dict(released) == {}
    assert released.last_seq == 2
    assert held[a].owner == "alice"  # provenance of the original claim, not current name
    assert (
        project_verified_transitions(
            (
                transition(1, owner="alice", incarnation="birth-1", claims=(a,), generation=G1),
                renamed_release,
            )
        )
        == released
    )
    reacquired = apply_transition(
        released,
        transition(3, owner="bob", incarnation="birth-2", claims=(a,), generation=G2),
    )
    for stale in (
        transition(4, owner="alice-new", incarnation="birth-1", releases=(ClaimRelease(a, G1),)),
        transition(4, owner="alice", incarnation="birth-3", releases=(ClaimRelease(a, G2),)),
        transition(4, owner="bob", incarnation="birth-2", releases=(ClaimRelease(a, G1),)),
    ):
        with pytest.raises(ClaimTransitionError, match="exact owner"):
            apply_transition(reacquired, stale)
    assert reacquired[a].owner == "bob" and reacquired[a].generation == G2


def test_verified_row_replay_is_deterministic_and_requires_order(tmp_path):
    _, a, b = resources(tmp_path)
    rows = (
        transition(1, claims=(a,), generation=G1),
        transition(7, claims=(b,), generation=G2),
        transition(8, releases=(ClaimRelease(a, G1),)),
    )
    first = project_verified_transitions(rows)
    assert dict(first) == dict(project_verified_transitions(tuple(rows)))
    assert list(first) == [b]
    assert first[b].seq == 7
    for invalid in (rows + (rows[-1],), (rows[1], rows[0]), (rows[0], object())):
        with pytest.raises(ClaimTransitionError):
            project_verified_transitions(invalid)
    with pytest.raises(ClaimTransitionError):
        apply_transition(first, "not a typed transition")


def test_only_exact_typed_nonempty_sequenced_envelopes(tmp_path):
    _, a, _ = resources(tmp_path)
    invalid = (
        lambda: transition(True, claims=(a,), generation=G1),
        lambda: transition(0, claims=(a,), generation=G1),
        lambda: transition(1),
        lambda: transition(1, claims=(a,), generation=None),
        lambda: transition(1, claims=(a,), generation="f" * 31),
        lambda: transition(1, releases=(ClaimRelease(a, G1),), generation=G2),
        lambda: transition(1, claims=(a,), generation=G1.upper()),
        lambda: ClaimTransition("owner", "epoch", 1, "m", (a,), ("bad",), G1),
        lambda: ClaimTransition("owner", "epoch", 1, "m", [a], (), G1),
    )
    for candidate in invalid:
        with pytest.raises(ClaimTransitionError):
            candidate()


def test_path_aliases_are_rejected_as_duplicate_normalized_resources(tmp_path):
    worktree, a, _ = resources(tmp_path)
    duplicate = (
        ExistingFileClaim(Path("a.py")).normalized(worktree),
        ExistingFileClaim(Path(worktree / "a.py")).normalized(worktree),
    )
    assert duplicate == (a, a)
    with pytest.raises(ClaimTransitionError, match="Duplicate"):
        transition(1, claims=duplicate, generation=G1)


def test_raw_path_aliases_cannot_acquire_or_release_same_file(tmp_path):
    worktree, a, _ = resources(tmp_path)
    held = apply_transition(ClaimProjection(), transition(1, claims=(a,), generation=G1))
    aliases = (f"{worktree}//a.py", f"{worktree}/./a.py")
    for alias in aliases:
        assert alias != a
        with pytest.raises(ClaimTransitionError, match="canonical absolute"):
            transition(2, owner="competitor", claims=(alias,), generation=G2)
        with pytest.raises(ClaimTransitionError, match="canonical absolute"):
            ClaimRelease(alias, G1)
        claiming_row = FieldCodec.encode(
            transition(2, owner="competitor", claims=(a,), generation=G2)
        )
        claiming_row["claims"] = [alias]
        with pytest.raises(ClaimTransitionError, match="canonical absolute"):
            FieldCodec.decode(ClaimTransition, claiming_row)
        releasing_row = FieldCodec.encode(transition(2, releases=(ClaimRelease(a, G1),)))
        releasing_row["releases"][0]["resource"] = alias
        with pytest.raises(ClaimTransitionError, match="canonical absolute"):
            FieldCodec.decode(ClaimTransition, releasing_row)
        assert list(held) == [a] and held[a].owner == "owner" and held.last_seq == 1
    with pytest.raises(ClaimConflict) as loser:
        apply_transition(held, transition(2, owner="competitor", claims=(a,), generation=G2))
    assert loser.value.existing.resource == a
    assert ExistingFileClaim(Path(aliases[0])).normalized(worktree) == a


def test_no_claim_state_or_sidecar_is_written_by_pure_projection(tmp_path):
    worktree, a, _ = resources(tmp_path)
    before = set(worktree.iterdir())
    assert (
        project_verified_transitions((transition(1, claims=(a,), generation=G1),))[a].owner
        == "owner"
    )
    assert set(worktree.iterdir()) == before
    assert os.access(worktree / "a.py", os.R_OK)
