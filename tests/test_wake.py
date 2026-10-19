"""Shadow-only wake and exact-route decisions; live compatibility stays unchanged."""

from dataclasses import replace
from pathlib import Path

import pytest
from typing import Type

from agent_comms.audience_manifest import FrozenAudience, FrozenRecipient, freeze_audience
from agent_comms.comms import wire
from agent_comms.wake_policy import MessageAudience
from agent_comms.mentions import ThreadMention
from agent_comms.messages import (
    AckKind, HandoffKind, InfoKind, MembershipChange, MembershipConversationRole, Message, MessageKind,
    NoticeRole,
)
from agent_comms.obligation_states import SilentResponse
from agent_comms.response_policy import CollectivePolicy, InformationalPolicy, MentionedOnlyPolicy
from agent_comms.thread_identity import AgentThreadRole, ThreadRole, UserThreadRole
from agent_comms.threads import Thread
from agent_comms.wake import (
    ControlClassification,
    resolve_wake,
    resolve_wake_cohort,
)
from agent_comms.wake_policy import BoundedTriageWake, FullWake, PassiveWake, NoWakeDecision, NoWakeReason, RecipientDecision, WakeDecision

MEMBERS = (FrozenRecipient("lookup-alpha", "alpha"), FrozenRecipient("lookup-beta", "beta"))


def stored(
    target: str = "#team",
    body: str = "question for the team",
    *,
    sender_role: Type[ThreadRole] = AgentThreadRole,
    mentions: tuple[ThreadMention, ...] = (),
    type: Type[MessageKind] = InfoKind,
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
) -> RecipientDecision | None:
    recipient = next(
        (member for member in members if member.recipient_lookup == lookup),
        FrozenRecipient(lookup, "outsider"),
    )
    return resolve_wake(
        message, recipient=recipient, frozen_audience=audience(message, members), control=control
    )


@pytest.mark.parametrize(
    ("sender_role", "policy", "starts_turn"),
    [(UserThreadRole, CollectivePolicy, True), (AgentThreadRole, InformationalPolicy, False)],
)
@pytest.mark.parametrize("type", [InfoKind, AckKind, HandoffKind])
def test_ordinary_unmentioned_channel_reaches_all_members_without_full_turns(
    sender_role: Type[ThreadRole], policy, starts_turn: bool, type: Type[MessageKind]
) -> None:
    message = stored(sender_role=sender_role, type=type)
    decisions = [decide(message, member.recipient_lookup) for member in MEMBERS]
    assert decisions == [
        WakeDecision(member.recipient_lookup, MessageAudience.COLLECTIVE, BoundedTriageWake())
        for member in MEMBERS
    ]
    assert decide(message, "lookup-outsider") is None
    assert message.target == "#team"
    # Shadow does not change the existing sender-role-dependent live behavior.
    assert message.role.response_policy(message) is policy.instance()
    assert message.role.response_policy(message).starts_turn_for(message, "alpha") is starts_turn


@pytest.mark.parametrize(("sender_role", "reply_target"), [(UserThreadRole, "sender"), (AgentThreadRole, None)])
def test_direct_is_full_only_for_frozen_lookup_and_route_ignores_sender_role(
    sender_role: Type[ThreadRole], reply_target: str | None,
) -> None:
    message = stored("alpha", sender_role=sender_role)
    members = MEMBERS[:1]
    assert decide(message, "lookup-alpha", members) == WakeDecision(
        "lookup-alpha", MessageAudience.DIRECT, FullWake()
    )
    assert decide(message, "lookup-beta", members) is None
    assert message.exact_reply_target == "sender"
    assert message.reply_target == reply_target


def test_valid_mention_selects_canonical_name_and_leaves_other_member_unaddressed() -> None:
    message = stored("#team", "@beta investigate", mentions=(ThreadMention("beta", 0, 5),))
    assert decide(message, "lookup-beta") == WakeDecision(
        "lookup-beta", MessageAudience.MENTIONED, FullWake()
    )
    assert decide(message, "lookup-alpha") == NoWakeDecision("lookup-alpha")
    assert decide(message, "lookup-outsider") is None  # Not in the frozen audience.
    assert MEMBERS[0] in audience(message).recipients  # The bus must retain both identities.
    assert message.exact_reply_target == "#team"
    assert message.role.response_policy(message) is MentionedOnlyPolicy.instance()
    assert message.role.response_policy(message).starts_turn_for(message, "beta")
    assert not message.role.response_policy(message).starts_turn_for(message, "alpha")
    assert message.reply_target is None


def test_renamed_mentioned_member_preserves_stable_lookup_not_old_alias() -> None:
    members = (FrozenRecipient("stable-alpha", "alpha"), FrozenRecipient("stable-beta", "renamed"))
    message = stored("#team", "@beta investigate", mentions=(ThreadMention("renamed", 0, 5),))
    assert decide(message, "stable-beta", members) == WakeDecision(
        "stable-beta", MessageAudience.MENTIONED, FullWake()
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
            member.recipient_lookup, MessageAudience.COLLECTIVE, BoundedTriageWake()
        )


def test_mixed_in_and_out_of_scope_mentions_only_name_valid_frozen_member() -> None:
    message = stored(
        "#team",
        "@outsider and @beta act",
        mentions=(ThreadMention("outsider", 0, 9), ThreadMention("beta", 14, 19)),
    )
    assert decide(message, "lookup-beta") == WakeDecision(
        "lookup-beta", MessageAudience.MENTIONED, FullWake()
    )
    assert decide(message, "lookup-alpha") == NoWakeDecision("lookup-alpha")
    assert MEMBERS[0] in audience(message).recipients


@pytest.mark.parametrize("control", list(ControlClassification))
@pytest.mark.parametrize("role", [NoticeRole(), MembershipConversationRole(MembershipChange.JOINED)])
def test_notice_and_membership_are_passive_regardless_of_control_or_sender(
    role, control: ControlClassification
) -> None:
    message = replace(stored(sender_role=AgentThreadRole), role=role)
    assert decide(message, control=control) == WakeDecision(
        "lookup-alpha", MessageAudience.COLLECTIVE, PassiveWake()
    )


def test_explicit_control_on_mentioned_row_is_passive_not_unmentioned_observer() -> None:
    message = stored("#team", "@beta investigate", mentions=(ThreadMention("beta", 0, 5),))
    for member in MEMBERS:
        assert decide(
            message, member.recipient_lookup, control=ControlClassification.SYSTEM_CONTROL
        ) == WakeDecision(member.recipient_lookup, MessageAudience.MENTIONED, PassiveWake())


def test_system_control_must_be_explicit_not_guessed_from_body_or_ack_type() -> None:
    ordinary = stored("#team", "system command", type=AckKind)
    assert decide(ordinary) == WakeDecision(
        "lookup-alpha", MessageAudience.COLLECTIVE, BoundedTriageWake()
    )
    assert decide(ordinary, control=ControlClassification.SYSTEM_CONTROL) == WakeDecision(
        "lookup-alpha", MessageAudience.COLLECTIVE, PassiveWake()
    )
    direct = stored("alpha", sender_role=UserThreadRole)
    assert decide(direct, members=MEMBERS[:1], control=ControlClassification.SYSTEM_CONTROL) == (
        WakeDecision("lookup-alpha", MessageAudience.DIRECT, PassiveWake())
    )
    with pytest.raises(TypeError, match="ControlClassification"):
        resolve_wake(
            ordinary,
            recipient=MEMBERS[0],
            frozen_audience=audience(ordinary),
            control="system_control",  # type: ignore[arg-type]
        )


def test_no_wake_is_not_a_passive_claim() -> None:
    mentioned = stored("#team", "@beta investigate", mentions=(ThreadMention("beta", 0, 5),))
    observer = decide(mentioned, "lookup-alpha")
    assert observer == NoWakeDecision("lookup-alpha", NoWakeReason.UNMENTIONED_OBSERVER)
    assert observer.claim_decisions() == ()
    assert observer.wake_policy() == PassiveWake()
    assert not hasattr(observer, "wake_mode")
    assert not hasattr(observer, "audience")
    assert MEMBERS[0] in audience(mentioned).recipients
    assert decide(mentioned, "lookup-outsider") is None
    passive = WakeDecision("lookup-alpha", MessageAudience.DIRECT, PassiveWake())
    assert passive.claim_decisions() == (passive,)
    assert passive.wake_policy() == observer.wake_policy()
    assert passive.to_wire() != observer.to_wire()


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


@pytest.mark.parametrize("changes", [
    {"mentions": ()},
    pytest.param({"role": NoticeRole()}, id="notice"),
    pytest.param({"role": MembershipConversationRole(MembershipChange.JOINED)}, id="membership"),
    {"type": HandoffKind},
    {"sender_role": UserThreadRole},
])
def test_same_message_id_does_not_override_frozen_full_envelope(changes) -> None:
    message = stored("#team", "@beta investigate", mentions=(ThreadMention("beta", 0, 5),))
    manifest = audience(message)
    changed = replace(message, **changes)
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
    import agent_comms.wake as wake_module

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
    assert decisions[-1] == WakeDecision("lookup-1023", MessageAudience.MENTIONED, FullWake())


def test_full_cohort_preserves_direct_control_and_fail_closed_envelope() -> None:
    direct = stored("alpha", sender_role=UserThreadRole)
    assert resolve_wake_cohort(
        direct,
        frozen_audience=audience(direct, MEMBERS[:1]),
        control=ControlClassification.ORDINARY,
    ) == (WakeDecision("lookup-alpha", MessageAudience.DIRECT, FullWake()),)
    collective = stored()
    assert resolve_wake_cohort(
        collective,
        frozen_audience=audience(collective),
        control=ControlClassification.SYSTEM_CONTROL,
    ) == tuple(
        WakeDecision(member.recipient_lookup, MessageAudience.COLLECTIVE, PassiveWake())
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
            replace(collective, role=NoticeRole()),
            frozen_audience=audience(collective),
            control=ControlClassification.ORDINARY,
        )


def test_exact_stored_channel_routes_no_wildcard_or_silence_inference(
    tmp_path: Path,
) -> None:
    comms = wire(tmp_path / "wire")
    for name in ("sender", "alpha", "beta"):
        comms.registry.declare(comms.registry.new_thread(name, frozenset({'team'}), str(tmp_path)))
    channel = comms.messaging.send("sender", "#team", "channel note").message
    broadcast = comms.messaging.send("sender", "#all", "everyone please respond").message
    dm = comms.messaging.send("sender", "alpha", "direct note").message
    assert broadcast.target == "#all"
    assert [item.exact_reply_target for item in (channel, broadcast, dm)] == [
        "#team",
        "#all",
        "sender",
    ]
    assert decide(broadcast) == WakeDecision(
        "lookup-alpha", MessageAudience.COLLECTIVE, BoundedTriageWake()
    )
    assert SilentResponse.declared_name == "silent"  # Existing explicit disposition type.
    assert channel.exact_reply_target == "#team"


def test_pure_shadow_does_not_mutate_envelope_state_or_live_cursor(tmp_path: Path) -> None:
    comms = wire(tmp_path / "wire")
    for name in ("sender", "alpha"):
        comms.registry.declare(comms.registry.new_thread(name, frozenset({'team'}), str(tmp_path)))
    message = comms.messaging.send("sender", "#team", "status update").message
    manifest = audience(message, MEMBERS[:1])
    before = message.to_wire()
    bus_before = comms.bus.log.path.read_bytes()
    high_water = comms.bus.log.latest_sequence()
    pending = comms.bus.pending_count("alpha")
    live_policy = message.role.response_policy(message)
    live_starts = message.role.response_policy(message).starts_turn_for(message, "alpha")
    live_reply = message.reply_target
    assert resolve_wake(
        message,
        recipient=MEMBERS[0],
        frozen_audience=manifest,
        control=ControlClassification.ORDINARY,
    ) == WakeDecision("lookup-alpha", MessageAudience.COLLECTIVE, BoundedTriageWake())
    assert message.exact_reply_target == "#team"
    assert message.to_wire() == before
    assert comms.bus.log.path.read_bytes() == bus_before
    assert comms.bus.log.latest_sequence() == high_water
    assert comms.bus.pending_count("alpha") == pending
    assert (message.role.response_policy(message), message.role.response_policy(message).starts_turn_for(message, "alpha"), message.reply_target) == (
        live_policy,
        live_starts,
        live_reply,
    )
