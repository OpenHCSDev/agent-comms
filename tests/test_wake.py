"""Shadow-only wake and exact-route decisions; live compatibility stays unchanged."""

from dataclasses import replace
from pathlib import Path

import pytest
from typing import Type

from agent_comms.audience_manifest import FrozenAudience, FrozenRecipient, freeze_audience
from conftest import installed_comms
from agent_comms.wake_policy import MessageAudience
from agent_comms.field_codec import ThreadLookup
from agent_comms.mentions import ThreadMention
from agent_comms.messages import AckKind, HandoffKind, InfoKind, MembershipTransition, MembershipConversationRole, Message, MessageKind, NoticeRole, JoinedMembership
from agent_comms.obligation_states import SilentResponse
from agent_comms.response_policy import CollectivePolicy, InformationalPolicy, MentionedOnlyPolicy
from agent_comms.thread_identity import AgentThreadRole, ThreadRole, UserThreadRole
from agent_comms.wake import MessageControl, resolve_wake, resolve_wake_cohort, OrdinaryControl, SystemControl
from agent_comms.wake_policy import BoundedTriageWake, FullWake, PassiveWake, NoWakeDecision, NoWakeReason, RecipientDecision, WakeDecision
from agent_comms.threads import Thread

ALPHA_LOOKUP = ThreadLookup("a" * 32)
BETA_LOOKUP = ThreadLookup("b" * 32)
OUTSIDER_LOOKUP = ThreadLookup("c" * 32)
RENAMED_ALPHA_LOOKUP = ThreadLookup("d" * 32)
RENAMED_BETA_LOOKUP = ThreadLookup("e" * 32)
SENDER_LOOKUP = ThreadLookup("f" * 32)

MEMBERS = (FrozenRecipient(ALPHA_LOOKUP, "alpha"), FrozenRecipient(BETA_LOOKUP, "beta"))


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
        message, members, "source-revision-42", sender_lookup=SENDER_LOOKUP, sender_name="sender"
    )


def decide(
    message: Message,
    lookup: ThreadLookup = ALPHA_LOOKUP,
    members: tuple[FrozenRecipient, ...] = MEMBERS,
    control: type[MessageControl] = OrdinaryControl,
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
    assert decide(message, OUTSIDER_LOOKUP) is None
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
    assert decide(message, ALPHA_LOOKUP, members) == WakeDecision(
        ALPHA_LOOKUP, MessageAudience.DIRECT, FullWake()
    )
    assert decide(message, BETA_LOOKUP, members) is None
    assert message.exact_reply_target == "sender"
    assert message.reply_target == reply_target


def test_valid_mention_selects_canonical_name_and_leaves_other_member_unaddressed() -> None:
    message = stored("#team", "@beta investigate", mentions=(ThreadMention("beta", 0, 5),))
    assert decide(message, BETA_LOOKUP) == WakeDecision(
        BETA_LOOKUP, MessageAudience.MENTIONED, FullWake()
    )
    assert decide(message, ALPHA_LOOKUP) == NoWakeDecision(ALPHA_LOOKUP)
    assert decide(message, OUTSIDER_LOOKUP) is None  # Not in the frozen audience.
    assert MEMBERS[0] in audience(message).recipients  # The bus must retain both identities.
    assert message.exact_reply_target == "#team"
    assert message.role.response_policy(message) is MentionedOnlyPolicy.instance()
    assert message.role.response_policy(message).starts_turn_for(message, "beta")
    assert not message.role.response_policy(message).starts_turn_for(message, "alpha")
    assert message.reply_target is None


def test_renamed_mentioned_member_preserves_stable_lookup_not_old_alias() -> None:
    members = (
        FrozenRecipient(RENAMED_ALPHA_LOOKUP, "alpha"),
        FrozenRecipient(RENAMED_BETA_LOOKUP, "renamed"),
    )
    message = stored("#team", "@beta investigate", mentions=(ThreadMention("renamed", 0, 5),))
    assert decide(message, RENAMED_BETA_LOOKUP, members) == WakeDecision(
        RENAMED_BETA_LOOKUP, MessageAudience.MENTIONED, FullWake()
    )
    assert decide(message, RENAMED_ALPHA_LOOKUP, members) == NoWakeDecision(RENAMED_ALPHA_LOOKUP)
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
    assert decide(message, BETA_LOOKUP) == WakeDecision(
        BETA_LOOKUP, MessageAudience.MENTIONED, FullWake()
    )
    assert decide(message, ALPHA_LOOKUP) == NoWakeDecision(ALPHA_LOOKUP)
    assert MEMBERS[0] in audience(message).recipients


@pytest.mark.parametrize("control", MessageControl.members_with(MessageControl))
@pytest.mark.parametrize("role", [NoticeRole(), MembershipConversationRole(JoinedMembership)])
def test_notice_and_membership_are_passive_regardless_of_control_or_sender(
    role, control: type[MessageControl]
) -> None:
    message = replace(stored(sender_role=AgentThreadRole), role=role)
    assert decide(message, control=control) == WakeDecision(
        ALPHA_LOOKUP, MessageAudience.COLLECTIVE, PassiveWake()
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
        ALPHA_LOOKUP, MessageAudience.COLLECTIVE, BoundedTriageWake()
    )
    assert decide(ordinary, control=SystemControl) == WakeDecision(
        ALPHA_LOOKUP, MessageAudience.COLLECTIVE, PassiveWake()
    )
    direct = stored("alpha", sender_role=UserThreadRole)
    assert decide(direct, members=MEMBERS[:1], control=SystemControl) == (
        WakeDecision(ALPHA_LOOKUP, MessageAudience.DIRECT, PassiveWake())
    )
    from agent_comms.field_codec import FieldCodec, MalformedRecord

    assert FieldCodec.decode(type[MessageControl], "system_control") is SystemControl
    with pytest.raises(MalformedRecord):
        FieldCodec.decode(type[MessageControl], "invented_control")


def test_no_wake_is_not_a_passive_claim() -> None:
    mentioned = stored("#team", "@beta investigate", mentions=(ThreadMention("beta", 0, 5),))
    observer = decide(mentioned, ALPHA_LOOKUP)
    assert observer == NoWakeDecision(ALPHA_LOOKUP, NoWakeReason.UNMENTIONED_OBSERVER)
    assert observer.claim_decisions() == ()
    assert observer.wake_policy() == PassiveWake()
    assert not hasattr(observer, "wake_mode")
    assert not hasattr(observer, "audience")
    assert MEMBERS[0] in audience(mentioned).recipients
    assert decide(mentioned, OUTSIDER_LOOKUP) is None
    passive = WakeDecision(ALPHA_LOOKUP, MessageAudience.DIRECT, PassiveWake())
    assert passive.claim_decisions() == (passive,)
    assert passive.wake_policy() == observer.wake_policy()
    assert passive.to_wire() != observer.to_wire()


def test_excluded_recipient_and_bad_identity_fail_closed() -> None:
    message = stored()
    manifest = audience(message)
    assert (
        resolve_wake(
            message,
            recipient=FrozenRecipient(ALPHA_LOOKUP, "someone_else"),
            frozen_audience=manifest,
            control=OrdinaryControl,
        )
        is None
    )  # Lookup alone cannot steal another frozen name's decision.
    with pytest.raises(TypeError):
        resolve_wake(
            message,
            recipient="alpha",  # type: ignore[arg-type]
            frozen_audience=manifest,
            control=OrdinaryControl,
        )
    with pytest.raises(TypeError):
        resolve_wake(
            message,
            recipient=MEMBERS[0],
            frozen_audience=object(),  # type: ignore[arg-type]
            control=OrdinaryControl,
        )
    with pytest.raises(ValueError):
        decide(stored("alpha"), members=MEMBERS)
    with pytest.raises(ValueError):
        decide(stored("alpha"), BETA_LOOKUP, members=(MEMBERS[1],))
    with pytest.raises(ValueError):
        resolve_wake(
            replace(message, seq=0),
            recipient=MEMBERS[0],
            frozen_audience=manifest,
            control=OrdinaryControl,
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
    with pytest.raises(ValueError):
        resolve_wake(
            mismatched,
            recipient=MEMBERS[0],
            frozen_audience=manifest,
            control=OrdinaryControl,
        )


@pytest.mark.parametrize("changes", [
    {"mentions": ()},
    pytest.param({"role": NoticeRole()}, id="notice"),
    pytest.param({"role": MembershipConversationRole(JoinedMembership)}, id="membership"),
    {"type": HandoffKind},
    {"sender_role": UserThreadRole},
])
def test_same_message_id_does_not_override_frozen_full_envelope(changes) -> None:
    message = stored("#team", "@beta investigate", mentions=(ThreadMention("beta", 0, 5),))
    manifest = audience(message)
    changed = replace(message, **changes)
    assert changed.message_id == message.message_id
    assert changed.to_wire() != message.to_wire()
    with pytest.raises(ValueError):
        resolve_wake(
            changed,
            recipient=MEMBERS[0],
            frozen_audience=manifest,
            control=OrdinaryControl,
        )


def test_full_cohort_preserves_direct_control_and_fail_closed_envelope() -> None:
    direct = stored("alpha", sender_role=UserThreadRole)
    assert resolve_wake_cohort(
        direct,
        frozen_audience=audience(direct, MEMBERS[:1]),
        control=OrdinaryControl,
    ) == (WakeDecision(ALPHA_LOOKUP, MessageAudience.DIRECT, FullWake()),)
    collective = stored()
    assert resolve_wake_cohort(
        collective,
        frozen_audience=audience(collective),
        control=SystemControl,
    ) == tuple(
        WakeDecision(member.recipient_lookup, MessageAudience.COLLECTIVE, PassiveWake())
        for member in MEMBERS
    )
    with pytest.raises(ValueError):
        resolve_wake_cohort(
            direct,
            frozen_audience=audience(direct, (MEMBERS[1],)),
            control=OrdinaryControl,
        )
    with pytest.raises(ValueError):
        resolve_wake_cohort(
            replace(collective, role=NoticeRole()),
            frozen_audience=audience(collective),
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
        ALPHA_LOOKUP, MessageAudience.COLLECTIVE, BoundedTriageWake()
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
        message,
        recipient=MEMBERS[0],
        frozen_audience=manifest,
        control=OrdinaryControl,
    ) == WakeDecision(ALPHA_LOOKUP, MessageAudience.COLLECTIVE, BoundedTriageWake())
    assert message.exact_reply_target == "#team"
    assert message.to_wire() == before
    assert comms.bus.bus.revision() == high_water
    assert comms.bus.pending_count("alpha") == pending
    assert (message.role.response_policy(message), message.role.response_policy(message).starts_turn_for(message, "alpha"), message.reply_target) == (
        live_policy,
        live_starts,
        live_reply,
    )
