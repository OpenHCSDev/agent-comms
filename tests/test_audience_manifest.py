"""Pure precommit audience values; these tests assert no durable delivery proof."""

from dataclasses import FrozenInstanceError, replace

import pytest

from agent_comms.audience_manifest import (
    MAX_RECIPIENTS,
    MAX_WIRE_ENVELOPE_BYTES,
    FrozenAudience,
    FrozenRecipient,
    freeze_audience,
)
from agent_comms.field_codec import ThreadLookup
from agent_comms.mentions import ThreadMention
from agent_comms.messages import Message, AlertKind, InfoKind
from agent_comms.thread_identity import AgentThreadRole, UserThreadRole


ALPHA_LOOKUP = ThreadLookup("a" * 32)
BETA_LOOKUP = ThreadLookup("b" * 32)
SENDER_LOOKUP = ThreadLookup("f" * 32)


def message(**changes):
    fields = dict(
        sender="sender",
        target="#team",
        body="@alpha please review",
        type=InfoKind,
        sender_role=UserThreadRole,
        timestamp=1234.5,
        seq=11,
        mentions=(ThreadMention("alpha", 0, 6),),
    )
    fields.update(changes)
    return Message(**fields)


def freeze(msg=None, members=None, **changes):
    return freeze_audience(
        message() if msg is None else msg,
        (
            [FrozenRecipient(BETA_LOOKUP, "beta"), FrozenRecipient(ALPHA_LOOKUP, "alpha")]
            if members is None
            else members
        ),
        "registry+catalog:7",
        sender_lookup=SENDER_LOOKUP,
        sender_name="sender",
        **changes,
    )


def test_full_ordered_membership_ignores_mentions_and_has_two_unique_identities():
    result = freeze()
    assert result.wire_seq == 11
    assert result.message_id == message().message_id
    assert result.exact_target == "#team"
    assert result.sender_lookup == SENDER_LOOKUP
    assert result.sender_name == "sender"
    assert result.source_revision == "registry+catalog:7"
    assert result.recipients == (
        FrozenRecipient(ALPHA_LOOKUP, "alpha"),
        FrozenRecipient(BETA_LOOKUP, "beta"),
    )
    assert result.canonical_members == frozenset({"alpha", "beta"})
    assert not hasattr(result, "wake_mode")
    assert not hasattr(result, "claim_id")
    assert freeze(members=list(reversed(result.recipients))).digest == result.digest
    with pytest.raises(FrozenInstanceError):
        result.exact_target = "#other"
    with pytest.raises(ValueError):
        replace(result, source_revision="changed")


def test_full_envelope_hash_binds_body_type_role_mentions_sequence_and_target():
    base = freeze()
    alternatives = (
        message(body="@alpha please retry"),
        message(type=AlertKind),
        message(sender_role=AgentThreadRole),
        message(mentions=()),
        message(seq=12),
        message(target="#other"),
        message(timestamp=1234.6),
    )
    for changed in alternatives:
        other = freeze(changed)
        assert other.wire_envelope_digest != base.wire_envelope_digest
        assert other.digest != base.digest
    assert freeze(message(body="@alpha please review", mentions=())).recipients == base.recipients


def test_reusing_frozen_value_after_rename_retag_stop_does_not_resolve_members_again():
    original = freeze()
    mutable_later_registry = {"alpha": ("renamed", frozenset(), "stopped")}
    mutable_later_registry["beta"] = ("beta", frozenset({"other"}), "running")
    assert original.recipients[0].canonical_thread == "alpha"
    assert original.canonical_members == frozenset({"alpha", "beta"})
    assert freeze(members=list(original.recipients)).digest == original.digest
    assert freeze(members=[FrozenRecipient(ALPHA_LOOKUP, "renamed")]).digest != original.digest


def test_exact_stored_all_target_and_no_wildcard_inference():
    all_members = freeze(message(target="#all"))
    assert all_members.exact_target == "#all"
    assert len(all_members.recipients) == 2
    with pytest.raises(ValueError):
        freeze(message(target="#any"))
    # The pure value cannot classify a saved view. Stage3 must attest routability.
    assert freeze(message(target="#saved-view")).exact_target == "#saved-view"


@pytest.mark.parametrize(
    "members",
    [
        [FrozenRecipient(ALPHA_LOOKUP, "alpha"), FrozenRecipient(ALPHA_LOOKUP, "beta")],
        [FrozenRecipient(ALPHA_LOOKUP, "alpha"), FrozenRecipient(BETA_LOOKUP, "alpha")],
        [FrozenRecipient(SENDER_LOOKUP, "alpha")],
        [FrozenRecipient(ALPHA_LOOKUP, "sender")],
    ],
)
def test_duplicate_or_sender_identities_fail_closed(members):
    with pytest.raises(ValueError):
        freeze(members=members)


def test_wrong_sender_or_unassigned_message_cannot_be_a_frozen_value():
    with pytest.raises(ValueError):
        freeze_audience(message(), (), "rev", sender_lookup=SENDER_LOOKUP, sender_name="another")
    with pytest.raises(ValueError):
        freeze(message(seq=0))


def test_bounded_id_count_and_envelope_bytes_fail_closed():
    with pytest.raises(ValueError):
        FrozenRecipient("id", "bad name")
    with pytest.raises(ValueError):
        FrozenRecipient("x" * 257, "alpha")
    with pytest.raises(ValueError):
        freeze(
            members=[
                FrozenRecipient(f"id-{index}", f"member-{index}")
                for index in range(MAX_RECIPIENTS + 1)
            ]
        )
    with pytest.raises(ValueError):
        freeze(message(body="x" * (MAX_WIRE_ENVELOPE_BYTES + 1), mentions=()))
    with pytest.raises(ValueError):
        freeze_audience(message(), (), " ", sender_lookup=SENDER_LOOKUP, sender_name="sender")
    with pytest.raises(TypeError):
        freeze(members=["alpha"])


def test_nonfinite_wire_timestamp_cannot_be_hashed():
    with pytest.raises(ValueError):
        freeze(message(timestamp=float("nan")))


def test_empty_audience_is_a_distinct_structural_value_not_a_delivery_receipt():
    empty = freeze(members=[])
    assert isinstance(empty, FrozenAudience)
    assert empty.recipients == ()
    assert empty.canonical_members == frozenset()
    assert empty.digest != freeze().digest
