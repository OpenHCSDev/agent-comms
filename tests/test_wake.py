"""Shadow-only wake and exact-route decisions; live compatibility stays unchanged."""

from dataclasses import replace
from pathlib import Path

import pytest

from agent_comms.audience_manifest import FrozenAudience, FrozenRecipient, freeze_audience
from agent_comms.coordination import MessageAudience, ObligationState, WakeMode
from agent_comms.declarations import (
    MembershipChange,
    Message,
    MessageType,
    ResponsePolicy,
    Thread,
    ThreadRole,
)
from agent_comms.mentions import ThreadMention
from agent_comms.operations import wire
from agent_comms.wake import (
    ControlClassification,
    NoWakeDecision,
    NoWakeReason,
    WakeDecision,
    derive_exact_reply_target,
    resolve_wake,
    resolve_wake_cohort,
)

MEMBERS = (FrozenRecipient("lookup-alpha", "alpha"), FrozenRecipient("lookup-beta", "beta"))


def stored(
    target: str = "#team",
    body: str = "question for the team",
    *,
    sender_role: ThreadRole = ThreadRole.AGENT,
    mentions: tuple[ThreadMention, ...] = (),
    type: MessageType = MessageType.INFO,
) -> Message:
    return Message(
        sender="sender",
        target=target,
        body=body,
        type=type,
        seq=42,
        sender_role=sender_role,
        mentions=mentions,
    )


def audience(message: Message, members: tuple[FrozenRecipient, ...] = MEMBERS) -> FrozenAudience:
    return freeze_audience(
        message, members, "source-revision-42", sender_lookup="lookup-sender", sender_name="sender"
    )


def decide(
    message: Message,
    lookup: str = "lookup-alpha",
    members: tuple[FrozenRecipient, ...] = MEMBERS,
    control: ControlClassification = ControlClassification.ORDINARY,
) -> WakeDecision | NoWakeDecision | None:
    recipient = next(
        (member for member in members if member.recipient_lookup == lookup),
        FrozenRecipient(lookup, "outsider"),
    )
    return resolve_wake(
        message, recipient=recipient, frozen_audience=audience(message, members), control=control
    )


@pytest.mark.parametrize("sender_role", [ThreadRole.USER, ThreadRole.AGENT])
@pytest.mark.parametrize("type", [MessageType.INFO, MessageType.ACK, MessageType.HANDOFF])
def test_ordinary_unmentioned_channel_reaches_all_members_without_full_turns(
    sender_role: ThreadRole, type: MessageType
) -> None:
    message = stored(sender_role=sender_role, type=type)
    decisions = [decide(message, member.recipient_lookup) for member in MEMBERS]
    assert decisions == [
        WakeDecision(member.recipient_lookup, MessageAudience.COLLECTIVE, WakeMode.BOUNDED_TRIAGE)
        for member in MEMBERS
    ]
    assert decide(message, "lookup-outsider") is None
    assert message.target == "#team"
    # Shadow does not change the existing sender-role-dependent live behavior.
    assert message.response_policy is (
        ResponsePolicy.COLLECTIVE
        if sender_role is ThreadRole.USER
        else ResponsePolicy.INFORMATIONAL
    )
    assert message.starts_turn_for("alpha") is (sender_role is ThreadRole.USER)


@pytest.mark.parametrize("sender_role", [ThreadRole.USER, ThreadRole.AGENT])
def test_direct_is_full_only_for_frozen_lookup_and_route_ignores_sender_role(
    sender_role: ThreadRole,
) -> None:
    message = stored("alpha", sender_role=sender_role)
    members = MEMBERS[:1]
    assert decide(message, "lookup-alpha", members) == WakeDecision(
        "lookup-alpha", MessageAudience.DIRECT, WakeMode.FULL
    )
    assert decide(message, "lookup-beta", members) is None
    assert derive_exact_reply_target(message) == "sender"
    assert message.reply_target == ("sender" if sender_role is ThreadRole.USER else None)


def test_valid_mention_selects_canonical_name_and_leaves_other_member_unaddressed() -> None:
    message = stored("#team", "@beta investigate", mentions=(ThreadMention("beta", 0, 5),))
    assert decide(message, "lookup-beta") == WakeDecision(
        "lookup-beta", MessageAudience.MENTIONED, WakeMode.FULL
    )
    assert decide(message, "lookup-alpha") == NoWakeDecision("lookup-alpha")
    assert decide(message, "lookup-outsider") is None  # Not in the frozen audience.
    assert MEMBERS[0] in audience(message).recipients  # The bus must retain both identities.
    assert derive_exact_reply_target(message) == "#team"
    assert message.response_policy is ResponsePolicy.MENTIONED_ONLY
    assert message.starts_turn_for("beta") and not message.starts_turn_for("alpha")
    assert message.reply_target is None  # Existing live behavior, not shadow route.


def test_renamed_mentioned_member_preserves_stable_lookup_not_old_alias() -> None:
    members = (FrozenRecipient("stable-alpha", "alpha"), FrozenRecipient("stable-beta", "renamed"))
    message = stored("#team", "@beta investigate", mentions=(ThreadMention("renamed", 0, 5),))
    assert decide(message, "stable-beta", members) == WakeDecision(
        "stable-beta", MessageAudience.MENTIONED, WakeMode.FULL
    )
    assert decide(message, "stable-alpha", members) == NoWakeDecision("stable-alpha")
    assert members[0] in audience(message, members).recipients
    # The textual old alias and mutable current tags are not consulted.
    assert "beta" not in audience(message, members).canonical_members


@pytest.mark.parametrize(
    "message",
    [
        stored("#team", "@unknown please investigate"),
        stored(
            "#team", "@outsider please investigate", mentions=(ThreadMention("outsider", 0, 9),)
        ),
        stored("#team", "@all everyone please investigate"),
    ],
)
def test_unknown_or_out_of_scope_mention_never_suppresses_frozen_members(message: Message) -> None:
    for member in MEMBERS:
        assert decide(message, member.recipient_lookup) == WakeDecision(
            member.recipient_lookup, MessageAudience.COLLECTIVE, WakeMode.BOUNDED_TRIAGE
        )


def test_mixed_in_and_out_of_scope_mentions_only_name_valid_frozen_member() -> None:
    message = stored(
        "#team",
        "@outsider and @beta act",
        mentions=(ThreadMention("outsider", 0, 9), ThreadMention("beta", 14, 19)),
    )
    assert decide(message, "lookup-beta") == WakeDecision(
        "lookup-beta", MessageAudience.MENTIONED, WakeMode.FULL
    )
    assert decide(message, "lookup-alpha") == NoWakeDecision("lookup-alpha")
    assert MEMBERS[0] in audience(message).recipients


@pytest.mark.parametrize("control", list(ControlClassification))
@pytest.mark.parametrize("kind", ["notice", "membership"])
def test_notice_and_membership_are_passive_regardless_of_control_or_sender(
    kind: str, control: ControlClassification
) -> None:
    message = stored(sender_role=ThreadRole.AGENT)
    if kind == "notice":
        message = replace(message, notice=True)
    else:
        message = replace(message, membership=MembershipChange.JOINED)
    assert decide(message, control=control) == WakeDecision(
        "lookup-alpha", MessageAudience.COLLECTIVE, WakeMode.PASSIVE
    )


def test_explicit_control_on_mentioned_row_is_passive_not_unmentioned_observer() -> None:
    message = stored("#team", "@beta investigate", mentions=(ThreadMention("beta", 0, 5),))
    for member in MEMBERS:
        assert decide(
            message, member.recipient_lookup, control=ControlClassification.SYSTEM_CONTROL
        ) == WakeDecision(member.recipient_lookup, MessageAudience.MENTIONED, WakeMode.PASSIVE)


def test_system_control_must_be_explicit_not_guessed_from_body_or_ack_type() -> None:
    ordinary = stored("#team", "system command", type=MessageType.ACK)
    assert decide(ordinary) == WakeDecision(
        "lookup-alpha", MessageAudience.COLLECTIVE, WakeMode.BOUNDED_TRIAGE
    )
    assert decide(ordinary, control=ControlClassification.SYSTEM_CONTROL) == WakeDecision(
        "lookup-alpha", MessageAudience.COLLECTIVE, WakeMode.PASSIVE
    )
    direct = stored("alpha", sender_role=ThreadRole.USER)
    assert decide(direct, members=MEMBERS[:1], control=ControlClassification.SYSTEM_CONTROL) == (
        WakeDecision("lookup-alpha", MessageAudience.DIRECT, WakeMode.PASSIVE)
    )
    with pytest.raises(TypeError, match="ControlClassification"):
        resolve_wake(
            ordinary,
            recipient=MEMBERS[0],
            frozen_audience=audience(ordinary),
            control="system_control",  # type: ignore[arg-type]
        )


def test_no_wake_is_not_a_passive_claim_and_decisions_are_strictly_typed() -> None:
    mentioned = stored("#team", "@beta investigate", mentions=(ThreadMention("beta", 0, 5),))
    observer = decide(mentioned, "lookup-alpha")
    assert observer == NoWakeDecision("lookup-alpha", NoWakeReason.UNMENTIONED_OBSERVER)
    assert isinstance(observer, NoWakeDecision)
    assert not hasattr(observer, "wake_mode")
    assert not hasattr(observer, "audience")
    assert MEMBERS[0] in audience(mentioned).recipients
    assert decide(mentioned, "lookup-outsider") is None
    with pytest.raises(ValueError, match="stable lookup"):
        WakeDecision("", MessageAudience.DIRECT, WakeMode.FULL)
    with pytest.raises(ValueError, match="stable lookup"):
        NoWakeDecision("")
    with pytest.raises(TypeError, match="MessageAudience and WakeMode"):
        WakeDecision("lookup-alpha", "direct", WakeMode.FULL)  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="MessageAudience and WakeMode"):
        WakeDecision("lookup-alpha", MessageAudience.DIRECT, "full")  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="NoWakeReason"):
        NoWakeDecision("lookup-alpha", "unmentioned_observer")  # type: ignore[arg-type]


def test_excluded_recipient_and_bad_identity_fail_closed() -> None:
    message = stored()
    manifest = audience(message)
    assert (
        resolve_wake(
            message,
            recipient=FrozenRecipient("lookup-alpha", "someone_else"),
            frozen_audience=manifest,
            control=ControlClassification.ORDINARY,
        )
        is None
    )  # Lookup alone cannot steal another frozen name's decision.
    with pytest.raises(TypeError, match="FrozenAudience and FrozenRecipient"):
        resolve_wake(
            message,
            recipient="alpha",  # type: ignore[arg-type]
            frozen_audience=manifest,
            control=ControlClassification.ORDINARY,
        )
    with pytest.raises(TypeError, match="FrozenAudience and FrozenRecipient"):
        resolve_wake(
            message,
            recipient=MEMBERS[0],
            frozen_audience=object(),  # type: ignore[arg-type]
            control=ControlClassification.ORDINARY,
        )
    with pytest.raises(ValueError, match="one frozen"):
        decide(stored("alpha"), members=MEMBERS)
    with pytest.raises(ValueError, match="direct frozen recipient"):
        decide(stored("alpha"), "lookup-beta", members=(MEMBERS[1],))
    with pytest.raises(ValueError, match="stored canonical"):
        resolve_wake(
            replace(message, seq=0),
            recipient=MEMBERS[0],
            frozen_audience=manifest,
            control=ControlClassification.ORDINARY,
        )
    with pytest.raises(ValueError, match="stored canonical"):
        derive_exact_reply_target(stored("broadcast"))


@pytest.mark.parametrize("field", ["seq", "body", "target", "sender"])
def test_prepared_message_must_match_frozen_manifest_identity(field: str) -> None:
    message = stored()
    manifest = audience(message)
    if field == "seq":
        mismatched = replace(message, seq=43)
    elif field == "body":
        mismatched = replace(message, body="different body")
    elif field == "target":
        mismatched = replace(message, target="#other")
    else:
        mismatched = replace(message, sender="other")
    with pytest.raises(ValueError, match="frozen audience identity"):
        resolve_wake(
            mismatched,
            recipient=MEMBERS[0],
            frozen_audience=manifest,
            control=ControlClassification.ORDINARY,
        )


@pytest.mark.parametrize("field", ["mentions", "notice", "membership", "type", "sender_role"])
def test_same_message_id_does_not_override_frozen_full_envelope(field: str) -> None:
    message = stored("#team", "@beta investigate", mentions=(ThreadMention("beta", 0, 5),))
    manifest = audience(message)
    replacements = {
        "mentions": (),
        "notice": True,
        "membership": MembershipChange.JOINED,
        "type": MessageType.HANDOFF,
        "sender_role": ThreadRole.USER,
    }
    changed = replace(message, **{field: replacements[field]})
    assert changed.message_id == message.message_id
    assert changed.to_wire() != message.to_wire()
    with pytest.raises(ValueError, match="frozen audience envelope"):
        resolve_wake(
            changed,
            recipient=MEMBERS[0],
            frozen_audience=manifest,
            control=ControlClassification.ORDINARY,
        )


def test_full_cohort_validates_large_frozen_n_with_one_envelope_digest(monkeypatch) -> None:
    from agent_comms import wake as wake_module

    members = tuple(
        FrozenRecipient(f"lookup-{index:04d}", f"member-{index:04d}") for index in range(1024)
    )
    body = "hello @member-1023"
    message = stored("#team", body, mentions=(ThreadMention("member-1023", 6, len(body)),))
    manifest = audience(message, members)
    original = wake_module.freeze_audience
    calls = 0

    def counted(*args, **kwargs):
        nonlocal calls
        calls += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(wake_module, "freeze_audience", counted)
    decisions = resolve_wake_cohort(
        message, frozen_audience=manifest, control=ControlClassification.ORDINARY
    )
    assert calls == 1  # Never rehash a 4MiB envelope once for every frozen recipient.
    assert len(decisions) == len(manifest.recipients)
    assert [decision.recipient for decision in decisions] == [
        member.recipient_lookup for member in manifest.recipients
    ]
    assert decisions[0] == NoWakeDecision("lookup-0000")
    assert decisions[-1] == WakeDecision("lookup-1023", MessageAudience.MENTIONED, WakeMode.FULL)


def test_full_cohort_preserves_direct_control_and_fail_closed_envelope() -> None:
    direct = stored("alpha", sender_role=ThreadRole.USER)
    assert resolve_wake_cohort(
        direct,
        frozen_audience=audience(direct, MEMBERS[:1]),
        control=ControlClassification.ORDINARY,
    ) == (WakeDecision("lookup-alpha", MessageAudience.DIRECT, WakeMode.FULL),)
    collective = stored()
    assert resolve_wake_cohort(
        collective,
        frozen_audience=audience(collective),
        control=ControlClassification.SYSTEM_CONTROL,
    ) == tuple(
        WakeDecision(member.recipient_lookup, MessageAudience.COLLECTIVE, WakeMode.PASSIVE)
        for member in MEMBERS
    )
    with pytest.raises(ValueError, match="direct frozen recipient"):
        resolve_wake_cohort(
            direct,
            frozen_audience=audience(direct, (MEMBERS[1],)),
            control=ControlClassification.ORDINARY,
        )
    with pytest.raises(ValueError, match="frozen audience envelope"):
        resolve_wake_cohort(
            replace(collective, notice=True),
            frozen_audience=audience(collective),
            control=ControlClassification.ORDINARY,
        )


def test_exact_stored_channel_and_alias_routes_no_wildcard_or_silence_inference(
    tmp_path: Path,
) -> None:
    comms = wire(tmp_path / "wire")
    for name in ("sender", "alpha", "beta"):
        comms.register(Thread(name, frozenset({"team"}), str(tmp_path)))
    channel = comms.send_message("sender", "#team", "channel note")
    broadcast = comms.send_message("sender", "broadcast", "everyone please respond")
    dm = comms.send_message("sender", "alpha", "direct note")
    assert broadcast.target == "#all"  # Alias was canonicalized by bus, not wake.
    assert [derive_exact_reply_target(item) for item in (channel, broadcast, dm)] == [
        "#team",
        "#all",
        "sender",
    ]
    assert derive_exact_reply_target(None) is None
    assert decide(broadcast) == WakeDecision(
        "lookup-alpha", MessageAudience.COLLECTIVE, WakeMode.BOUNDED_TRIAGE
    )
    assert ObligationState.SILENT.value == "silent"  # Existing explicit disposition type.
    assert derive_exact_reply_target(channel) == "#team"  # Route != response obligation.


def test_pure_shadow_does_not_mutate_envelope_state_or_live_cursor(tmp_path: Path) -> None:
    comms = wire(tmp_path / "wire")
    for name in ("sender", "alpha"):
        comms.register(Thread(name, frozenset({"team"}), str(tmp_path)))
    message = comms.send_message("sender", "#team", "status update")
    manifest = audience(message, MEMBERS[:1])
    before = message.to_wire()
    bus_before = comms.bus._path.read_bytes()
    high_water = comms.message_high_water()
    pending = comms.pending_count("alpha")
    live_policy = message.response_policy
    live_starts = message.starts_turn_for("alpha")
    live_reply = message.reply_target
    assert resolve_wake(
        message,
        recipient=MEMBERS[0],
        frozen_audience=manifest,
        control=ControlClassification.ORDINARY,
    ) == WakeDecision("lookup-alpha", MessageAudience.COLLECTIVE, WakeMode.BOUNDED_TRIAGE)
    assert derive_exact_reply_target(message) == "#team"
    assert message.to_wire() == before
    assert comms.bus._path.read_bytes() == bus_before
    assert comms.message_high_water() == high_water
    assert comms.pending_count("alpha") == pending
    assert (message.response_policy, message.starts_turn_for("alpha"), message.reply_target) == (
        live_policy,
        live_starts,
        live_reply,
    )
