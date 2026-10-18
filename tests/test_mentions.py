from agent_comms.messages import NoticeRole
import pytest
from agent_comms.tool_caller import NoThreadCaller

from agent_comms.mentions import MentionQuery
from agent_comms.comms import Comms, wire
from agent_comms.errors import RelationViolationError
from agent_comms.mentions import MentionCandidate, ThreadMention
from agent_comms.messages import Message, AlertKind, InfoKind
from agent_comms.response_policy import CollectivePolicy, InformationalPolicy, MentionedOnlyPolicy
from agent_comms.routing import ScheduledTurn
from agent_comms.thread_identity import AgentThreadRole, UserThreadRole
from agent_comms.threads import Thread


def test_mentions_are_addressees_without_changing_channel_delivery(tmp_path):
    comms = Comms(tmp_path)
    for name in ("alpha", "beta"):
        comms.registry.declare(comms.registry.new_thread(name, frozenset({'team'}), str(tmp_path)))
    message = comms.messaging.send_user_message(
        "#team", "@alpha please review; @beta FYI", worktree=str(tmp_path)
    )
    assert message.target == "#team"
    assert [item.thread for item in message.mentions] == ["alpha", "beta"]
    assert [message.body[item.start : item.end] for item in message.mentions] == ["@alpha", "@beta"]
    assert [item.message_id for item in comms.bus.inbox("alpha")] == [message.message_id]
    assert [item.message_id for item in comms.bus.inbox("beta")] == [message.message_id]
    assert list(Comms(tmp_path).views.channel_history_page("#team").messages)[0].mentions == message.mentions
    assert Message.from_wire(message.to_wire()) == message
    scheduled = ScheduledTurn.incoming(message)
    assert scheduled.origin.exact_reply_target == "#team"
    assert "Response policy: mentioned_only" in scheduled.prompt
    assert "only resolved mentioned identities may respond: @alpha, @beta" in scheduled.prompt
    assert "unmentioned observers dismiss quietly" in scheduled.prompt
    assert "delivery and history remain #team" in scheduled.prompt


def test_aliases_resolve_but_emails_paths_and_unknown_names_are_plain_text(tmp_path):
    comms = Comms(tmp_path)
    comms.registry.declare(comms.registry.new_thread('alpha', frozenset({'team'}), str(tmp_path)))
    comms.registry.rename("alpha", "renamed")
    message = comms.messaging.send_user_message(
        "#team",
        "mail@alpha /tmp/@alpha \\dir\\@alpha @@alpha @unknown (@alpha).",
        worktree=str(tmp_path),
    )
    assert len(message.mentions) == 1
    assert message.mentions[0].thread == "renamed"
    assert message.body[message.mentions[0].start : message.mentions[0].end] == "@alpha"


def test_channel_response_eligibility_is_typed_and_unknown_mentions_are_collective(tmp_path):
    comms = Comms(tmp_path)
    for name in ("alpha", "beta"):
        comms.registry.declare(comms.registry.new_thread(name, frozenset({'team'}), str(tmp_path)))

    collective = comms.messaging.send_user_message(
        "#team", "@unknown can anyone answer?", worktree=str(tmp_path)
    )
    assert not collective.mentions
    assert collective.role.response_policy(collective) is CollectivePolicy.instance()
    assert collective.response_eligibility(("alpha", "beta")).recipients == (
        "alpha",
        "beta",
    )
    assert collective.role.response_policy(collective).starts_turn_for(collective, "alpha")
    assert collective.role.response_policy(collective).starts_turn_for(collective, "beta")
    collective_prompt = ScheduledTurn.incoming(collective).prompt
    assert "Response policy: collective; channel members may respond" in collective_prompt
    assert "delivery and history remain #team" in collective_prompt

    mentioned = comms.messaging.send_user_message("#team", "@beta please answer", worktree=str(tmp_path))
    eligibility = mentioned.response_eligibility(("alpha", "beta"))
    assert eligibility.policy is MentionedOnlyPolicy.instance()
    assert eligibility.recipients == ("beta",)
    assert not mentioned.role.response_policy(mentioned).starts_turn_for(mentioned, "alpha")
    assert mentioned.role.response_policy(mentioned).starts_turn_for(mentioned, "beta")
    assert mentioned.target == "#team"
    mentioned_prompt = ScheduledTurn.incoming(mentioned).prompt
    assert "only resolved mentioned identities may respond: @beta" in mentioned_prompt
    assert "unmentioned observers dismiss quietly" in mentioned_prompt

    agent_chatter = comms.messaging.send_message("alpha", "#team", "status only")
    assert agent_chatter.role.response_policy(agent_chatter) is InformationalPolicy.instance()
    assert not agent_chatter.response_eligibility(("beta",)).recipients
    assert not agent_chatter.role.response_policy(agent_chatter).starts_turn_for(agent_chatter, "beta")
    agent_prompt = ScheduledTurn.incoming(agent_chatter).prompt
    assert "Response policy: informational; observe and dismiss without replying" in agent_prompt
    assert "delivery and history remain #team" in agent_prompt

    informational = comms.messaging.send_message("alpha", "#team", "notice only", role=NoticeRole())
    assert informational.role.response_policy(informational) is InformationalPolicy.instance()
    assert not informational.response_eligibility(("alpha", "beta")).recipients
    assert not informational.role.response_policy(informational).starts_turn_for(informational, "beta")
    assert "Response policy: informational; observe and dismiss without replying" in (
        ScheduledTurn.incoming(informational).prompt
    )


@pytest.mark.parametrize(
    ("sender_role", "unmentioned_policy"),
    (
        (UserThreadRole, CollectivePolicy.instance()),
        (AgentThreadRole, InformationalPolicy.instance()),
    ),
)
def test_unmentioned_channel_policy_uses_typed_sender_role(sender_role, unmentioned_policy):
    unmentioned = Message(
        "sender",
        "#team",
        "status for the team",
        InfoKind,
        sender_role=sender_role,
    )
    assert unmentioned.role.response_policy(unmentioned) is unmentioned_policy
    assert unmentioned.role.response_policy(unmentioned).starts_turn is (unmentioned_policy is CollectivePolicy.instance())
    assert unmentioned.role.response_policy(unmentioned).starts_turn_for(unmentioned, "alpha") is (
        unmentioned_policy is CollectivePolicy.instance()
    )
    assert unmentioned.target == "#team"

    mentioned = Message(
        "sender",
        "#team",
        "@beta investigate",
        InfoKind,
        sender_role=sender_role,
        mentions=(ThreadMention("beta", 0, 5),),
    )
    assert mentioned.role.response_policy(mentioned) is MentionedOnlyPolicy.instance()
    assert not mentioned.role.response_policy(mentioned).starts_turn
    assert not mentioned.role.response_policy(mentioned).starts_turn_for(mentioned, "alpha")
    assert mentioned.role.response_policy(mentioned).starts_turn_for(mentioned, "beta")
    assert mentioned.target == "#team"

    notice = Message(
        "sender",
        "#team",
        "delivery failed",
        AlertKind,
        sender_role=sender_role,
        role=NoticeRole(),
    )
    assert notice.role.response_policy(notice) is InformationalPolicy.instance()
    assert not notice.role.response_policy(notice).starts_turn_for(notice, "alpha")


def test_completion_uses_the_model_syntax_at_the_cursor():
    choices = (MentionCandidate("alpha", "Alpha"), MentionCandidate("beta", "Beta"))
    query = MentionQuery.at_cursor("ask @al now", 7)
    assert query is not None and (query.start, query.end) == (4, 7)
    assert query.candidates(choices) == choices[:1]
    assert MentionQuery.at_cursor("mail@al", 7) is None
    assert MentionQuery.at_cursor("ask @", 5).candidates(choices) == choices
    middle = MentionQuery.at_cursor("@alphax", 3)
    assert middle is not None and middle.end == 7


def test_failed_delivery_posts_a_non_waking_notice_to_the_origin(tmp_path):
    from agent_comms.messages import AlertKind, InfoKind

    comms = Comms(tmp_path)
    for name in ("sender", "owner"):
        comms.registry.declare(comms.registry.new_thread(name, frozenset({'team'}), str(tmp_path)))
    # A notice never wakes its recipient, so failure feedback cannot loop.
    comms.messaging.send("owner", "#team", "Delivery failed: usage limit", AlertKind, role=NoticeRole())
    notice = list(comms.views.channel_history_page("#team").messages)[-1]
    assert isinstance(notice.role, NoticeRole) and not notice.role.response_policy(notice).starts_turn
    with comms.bus.opened_activity() as clocks:
        assert "sender" not in comms.views.last_sent_timestamps(clocks.sent.items(), registry=comms.registry.snapshot())
    assert Message.from_wire(notice.to_wire()) == notice


def test_model_tool_changes_own_and_another_thread(comms, tmp_path, monkeypatch):
    from functools import partial
    from agent_comms.tools import invoke_tool
    from agent_comms.tool_caller import ThreadCaller

    invoke_tool = partial(invoke_tool, caller=ThreadCaller("owner"))

    comms.registry.declare(comms.registry.new_thread('owner', frozenset(), str(tmp_path)))
    comms.registry.declare(comms.registry.new_thread('peer', frozenset(), str(tmp_path)))
    monkeypatch.setenv("AGENT_COMMS_THREAD", "owner")
    result = invoke_tool(comms, "comms_model", {"model": "openrouter/own"})
    assert result == {
        "thread": "owner",
        "model": "openrouter/own",
        "thinking_level": None,
        "service_tier": "priority",
    }
    peer_configuration = {
        "thread": "peer",
        "model": "openai-codex/gpt-5.6-sol",
        "thinking_level": "high",
        "service_tier": "standard",
    }
    with pytest.raises(RelationViolationError, match="through its own session"):
        invoke_tool(comms, "comms_model", peer_configuration)
    comms.registry.unregister("peer")
    result = invoke_tool(comms, "comms_model", peer_configuration)
    assert result["thread"] == "peer" and result["thinking_level"] == "high"
    configured = comms.registry.require("peer")
    assert configured.model == "openai-codex/gpt-5.6-sol"
    assert configured.service_tier.value == "standard"
    with pytest.raises(ValueError, match="not a valid ServiceTier"):
        invoke_tool(comms, "comms_model", {"model": "openrouter/own", "service_tier": "fast"})


def test_targeted_inbox_returns_mentions_and_advances_only_own_cursor(comms, tmp_path):
    from agent_comms.tools import invoke_tool

    for name in ("alpha", "beta"):
        comms.registry.declare(comms.registry.new_thread(name, frozenset({'team'}), str(tmp_path)))
    comms.messaging.send_user_message("#team", "@alpha please review", worktree=str(tmp_path))
    assert comms.bus.pending_count("alpha", "#team") == 1
    assert comms.bus.pending_count("beta", "#team") == 1
    result = invoke_tool(
        comms, "comms_inbox", {"thread": "beta", "target": "#team"}, caller=NoThreadCaller()
    )
    assert [message["text"] for message in result["messages"]] == ["@alpha please review"]
    assert result["messages"][0]["mentions"][0]["thread"] == "alpha"
    assert result["acknowledged"] == 1
    # beta no longer sees the ping; alpha's delivery is untouched.
    assert comms.bus.pending_count("beta", "#team") == 0
    assert comms.bus.pending_count("alpha", "#team") == 1


def test_inbox_rejects_an_empty_target(tmp_path):
    import pytest

    from agent_comms.tools import invoke_tool

    comms = Comms(tmp_path)
    comms.registry.declare(comms.registry.new_thread('beta', frozenset({'team'}), str(tmp_path)))
    with pytest.raises(ValueError, match="inbox target"):
        invoke_tool(comms, "comms_inbox", {"thread": "beta", "target": ""}, caller=NoThreadCaller())


def test_committed_channel_mention_follows_recipient_renames_without_expanding_audience(tmp_path):
    comms = Comms(tmp_path)
    for name, tags in [("alpha", {"team"}), ("observer", {"team"}), ("outsider", set())]:
        comms.registry.declare(comms.registry.new_thread(name, frozenset(tags), str(tmp_path)))
    message = comms.messaging.send_user_message("#team", "@alpha please review", worktree=str(tmp_path))
    comms.registry.rename("alpha", "renamed")
    comms.registry.rename("renamed", "final")
    aliases = comms.registry.snapshot().aliases
    assert message in comms.bus.incoming_page("final", after=0).messages
    assert message.role.response_policy(message).starts_turn_for(message, "final", aliases=aliases)
    assert not message.role.response_policy(message).starts_turn_for(message, "observer", aliases=aliases)
    assert message.response_eligibility(("final", "observer"), aliases=aliases).recipients == (
        "final",
    )
    assert (
        "only resolved mentioned identities may respond: @final"
        in ScheduledTurn.incoming(message, aliases=aliases).prompt
    )
    assert message.mentions[0].thread == "alpha"  # Original wire record is unchanged.
    outside = comms.messaging.send_user_message("#team", "@outsider review", worktree=str(tmp_path))
    comms.registry.rename("outsider", "outside-renamed")
    assert not comms.bus.incoming_page("outside-renamed", after=0).messages
    assert (
        outside.response_eligibility(
            ("final", "observer"), aliases=comms.registry.snapshot().aliases
        ).recipients
        == ()
    )
