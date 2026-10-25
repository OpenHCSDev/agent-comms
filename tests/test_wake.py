"""Shadow-only wake and exact-route decisions; live compatibility stays unchanged."""

from dataclasses import replace
from pathlib import Path
from typing import Type

import pytest

from agent_comms.audience_manifest import AudiencedMessage, FrozenRecipient, freeze_audience
from agent_comms.field_codec import ThreadLookup
from agent_comms.mentions import ThreadMention
from agent_comms.messages import (
    AckKind,
    HandoffKind,
    InfoKind,
    JoinedMembership,
    MembershipConversationRole,
    MembershipTransition,
    Message,
    MessageKind,
    NoticeRole,
)
from agent_comms.obligation_states import SilentResponse
from agent_comms.response_policy import CollectivePolicy, InformationalPolicy, MentionedOnlyPolicy
from agent_comms.thread_identity import AgentThreadRole, ThreadRole, UserThreadRole
from agent_comms.threads import Thread
from agent_comms.wake import (
    MessageControl,
    OrdinaryControl,
    SystemControl,
    resolve_wake,
    resolve_wake_cohort,
)
from agent_comms.wake_policy import (
    BoundedTriageWake,
    FullWake,
    MessageAudience,
    NoWakeDecision,
    NoWakeReason,
    PassiveWake,
    RecipientDecision,
    WakeDecision,
)
from bus_record_fixture import (
    OWNER_LOOKUP,
    RECIPIENT_LOOKUP,
    RENAMED_OWNER_LOOKUP,
    RENAMED_RECIPIENT_LOOKUP,
    SENDER_LOOKUP,
    UNRELATED_LOOKUP,
)
from conftest import installed_comms

MEMBERS = (FrozenRecipient(SENDER_LOOKUP, "alpha"), FrozenRecipient(RECIPIENT_LOOKUP, "beta"))


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


def audience(message: Message, members: tuple[FrozenRecipient, ...] = MEMBERS) -> AudiencedMessage:
    return freeze_audience(
        message, members, "source-revision-42", sender_lookup=UNRELATED_LOOKUP, sender_name="sender"
    )


def decide(
    message: Message,
    lookup: ThreadLookup = SENDER_LOOKUP,
    members: tuple[FrozenRecipient, ...] = MEMBERS,
    control: type[MessageControl] = OrdinaryControl,
) -> RecipientDecision | None:
    recipient = next(
        (member for member in members if member.recipient_lookup == lookup),
        FrozenRecipient(lookup, "outsider"),
    )
    return resolve_wake(
        audience(message, members), recipient=recipient, control=control
    )


@pytest.mark.parametrize(
    ("sender_role", "policy", "starts_turn"),
    [(UserThreadRole, CollectivePolicy, True), (AgentThreadRole, InformationalPolicy, False)],
)
@pytest.mark.parametrize("type", [InfoKind, AckKind, HandoffKind])
def test_ordinary_unmentioned_channel_reaches_all_members_without_full_turns(
    sender_role: Type[ThreadRole], policy, starts_turn: bool, type: Type[MessageKind],
) -> None:
    message = stored(sender_role=sender_role, type=type)
    decisions = [decide(message, member.recipient_lookup) for member in MEMBERS]
    assert decisions == [
        WakeDecision(member.recipient_lookup, MessageAudience.COLLECTIVE, BoundedTriageWake())
        for member in MEMBERS
    ]
    assert decide(message, OWNER_LOOKUP) is None
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
    assert decide(message, SENDER_LOOKUP, members) == WakeDecision(
        SENDER_LOOKUP, MessageAudience.DIRECT, FullWake()
    )
    assert decide(message, RECIPIENT_LOOKUP, members) is None
    assert message.exact_reply_target == "sender"
    assert message.reply_target == reply_target


def test_valid_mention_selects_canonical_name_and_leaves_other_member_unaddressed() -> None:
    message = stored("#team", "@beta investigate", mentions=(ThreadMention("beta", 0, 5),))
    assert decide(message, RECIPIENT_LOOKUP) == WakeDecision(
        RECIPIENT_LOOKUP, MessageAudience.MENTIONED, FullWake()
    )
    assert decide(message, SENDER_LOOKUP) == NoWakeDecision(SENDER_LOOKUP)
    assert decide(message, OWNER_LOOKUP) is None  # Not in the frozen audience.
    assert MEMBERS[0] in audience(message).recipients  # The bus must retain both identities.
    assert message.exact_reply_target == "#team"
    assert message.role.response_policy(message) is MentionedOnlyPolicy.instance()
    assert message.role.response_policy(message).starts_turn_for(message, "beta")
    assert not message.role.response_policy(message).starts_turn_for(message, "alpha")
    assert message.reply_target is None


def test_renamed_mentioned_member_preserves_stable_lookup_not_old_alias() -> None:
    members = (
        FrozenRecipient(RENAMED_OWNER_LOOKUP, "alpha"),
        FrozenRecipient(RENAMED_RECIPIENT_LOOKUP, "renamed"),
    )
    message = stored("#team", "@beta investigate", mentions=(ThreadMention("renamed", 0, 5),))
    assert decide(message, RENAMED_RECIPIENT_LOOKUP, members) == WakeDecision(
        RENAMED_RECIPIENT_LOOKUP, MessageAudience.MENTIONED, FullWake()
    )
    assert decide(message, RENAMED_OWNER_LOOKUP, members) == NoWakeDecision(RENAMED_OWNER_LOOKUP)
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
    assert decide(message, RECIPIENT_LOOKUP) == WakeDecision(
        RECIPIENT_LOOKUP, MessageAudience.MENTIONED, FullWake()
    )
    assert decide(message, SENDER_LOOKUP) == NoWakeDecision(SENDER_LOOKUP)
    assert MEMBERS[0] in audience(message).recipients


@pytest.mark.parametrize("control", MessageControl.members_with(MessageControl))
@pytest.mark.parametrize("role", [NoticeRole(), MembershipConversationRole(JoinedMembership)])
def test_notice_and_membership_are_passive_regardless_of_control_or_sender(
    role, control: type[MessageControl]
) -> None:
    message = replace(stored(sender_role=AgentThreadRole), role=role)
    assert decide(message, control=control) == WakeDecision(
        SENDER_LOOKUP, MessageAudience.COLLECTIVE, PassiveWake()
    )


def test_explicit_control_on_mentioned_row_is_passive_not_unmentioned_observer() -> None:
    message = stored("#team", "@beta investigate", mentions=(ThreadMention("beta", 0, 5),))
    for member in MEMBERS:
        assert decide(
            message, member.recipient_lookup, control=SystemControl
        ) == WakeDecision(member.recipient_lookup, MessageAudience.MENTIONED, PassiveWake())


def test_system_control_must_be_explicit_not_guessed_from_body_or_ack_type() -> None:
    ordinary = stored("#team", "system command", type=AckKind)
    assert decide(ordinary) == WakeDecision(
        SENDER_LOOKUP, MessageAudience.COLLECTIVE, BoundedTriageWake()
    )
    assert decide(ordinary, control=SystemControl) == WakeDecision(
        SENDER_LOOKUP, MessageAudience.COLLECTIVE, PassiveWake()
    )
    direct = stored("alpha", sender_role=UserThreadRole)
    assert decide(direct, members=MEMBERS[:1], control=SystemControl) == (
        WakeDecision(SENDER_LOOKUP, MessageAudience.DIRECT, PassiveWake())
    )
    from agent_comms.field_codec import FieldCodec, MalformedRecord

    assert FieldCodec.decode(type[MessageControl], "system_control") is SystemControl
    with pytest.raises(MalformedRecord):
        FieldCodec.decode(type[MessageControl], "invented_control")


def test_no_wake_is_not_a_passive_claim() -> None:
    mentioned = stored("#team", "@beta investigate", mentions=(ThreadMention("beta", 0, 5),))
    observer = decide(mentioned, SENDER_LOOKUP)
    assert observer == NoWakeDecision(SENDER_LOOKUP, NoWakeReason.UNMENTIONED_OBSERVER)
    assert observer.claim_decisions() == ()
    assert observer.wake_policy() == PassiveWake()
    assert not hasattr(observer, "wake_mode")
    assert not hasattr(observer, "audience")
    assert MEMBERS[0] in audience(mentioned).recipients
    assert decide(mentioned, OWNER_LOOKUP) is None
    passive = WakeDecision(SENDER_LOOKUP, MessageAudience.DIRECT, PassiveWake())
    assert passive.claim_decisions() == (passive,)
    assert passive.wake_policy() == observer.wake_policy()
    assert passive.to_wire() != observer.to_wire()


def test_excluded_recipient_and_bad_identity_fail_closed() -> None:
    message = stored()
    manifest = audience(message)
    assert (
        resolve_wake(
            manifest,
            recipient=FrozenRecipient(SENDER_LOOKUP, "someone_else"),
            control=OrdinaryControl,
        )
        is None
    )  # Lookup alone cannot steal another frozen name's decision.
    with pytest.raises(ValueError):
        decide(stored("alpha"), members=MEMBERS)
    with pytest.raises(ValueError):
        decide(stored("alpha"), RECIPIENT_LOOKUP, members=(MEMBERS[1],))
    with pytest.raises(ValueError):
        resolve_wake(
            replace(manifest, seq=0),
            recipient=MEMBERS[0],
            control=OrdinaryControl,
        )


def test_full_cohort_preserves_direct_control_and_fail_closed_envelope() -> None:
    direct = stored("alpha", sender_role=UserThreadRole)
    assert resolve_wake_cohort(
        audience(direct, MEMBERS[:1]),
        control=OrdinaryControl,
    ) == (WakeDecision(SENDER_LOOKUP, MessageAudience.DIRECT, FullWake()),)
    collective = stored()
    assert resolve_wake_cohort(
        audience(collective),
        control=SystemControl,
    ) == tuple(
        WakeDecision(member.recipient_lookup, MessageAudience.COLLECTIVE, PassiveWake())
        for member in MEMBERS
    )
    with pytest.raises(ValueError):
        resolve_wake_cohort(
            audience(direct, (MEMBERS[1],)),
            control=OrdinaryControl,
        )


def test_exact_stored_channel_routes_no_wildcard_or_silence_inference(
    tmp_path: Path,
) -> None:
    comms = installed_comms(tmp_path / "wire")
    for name in ("sender", "alpha", "beta"):
        comms.registry.declare(Thread(name, frozenset({'team'}), str(tmp_path)))
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
        SENDER_LOOKUP, MessageAudience.COLLECTIVE, BoundedTriageWake()
    )
    assert SilentResponse.declared_name == "silent"  # Existing explicit disposition type.
    assert channel.exact_reply_target == "#team"


def test_pure_shadow_does_not_mutate_envelope_state_or_live_cursor(tmp_path: Path) -> None:
    comms = installed_comms(tmp_path / "wire")
    for name in ("sender", "alpha"):
        comms.registry.declare(Thread(name, frozenset({'team'}), str(tmp_path)))
    message = comms.messaging.send("sender", "#team", "status update").message
    manifest = audience(message, MEMBERS[:1])
    before = message.to_wire()
    high_water = comms.bus.bus.revision()
    pending = comms.bus.pending_count("alpha")
    live_policy = message.role.response_policy(message)
    live_starts = message.role.response_policy(message).starts_turn_for(message, "alpha")
    live_reply = message.reply_target
    assert resolve_wake(
        manifest,
        recipient=MEMBERS[0],
        control=OrdinaryControl,
    ) == WakeDecision(SENDER_LOOKUP, MessageAudience.COLLECTIVE, BoundedTriageWake())
    assert message.exact_reply_target == "#team"
    assert message.to_wire() == before
    assert comms.bus.bus.revision() == high_water
    assert comms.bus.pending_count("alpha") == pending
    assert (message.role.response_policy(message), message.role.response_policy(message).starts_turn_for(message, "alpha"), message.reply_target) == (
        live_policy,
        live_starts,
        live_reply,
    )
