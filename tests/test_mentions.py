import pytest

from agent_comms import (
    MentionCandidate,
    MentionQuery,
    Message,
    MessageType,
    Thread,
    ThreadMention,
    ThreadRole,
)
from agent_comms.comms import wire
from agent_comms.declarations import ScheduledTurn
from agent_comms.response_policy import CollectivePolicy, InformationalPolicy, MentionedOnlyPolicy


def test_mentions_are_addressees_without_changing_channel_delivery(tmp_path):
    comms = wire(tmp_path)
    for name in ("alpha", "beta"):
        comms.threads.register(Thread(name, frozenset({"team"}), str(tmp_path)))
    message = comms.messaging.send_user_message(
        "#team", "@alpha please review; @beta FYI", worktree=str(tmp_path)
    )
    assert message.target == "#team"
    assert [item.thread for item in message.mentions] == ["alpha", "beta"]
    assert [message.body[item.start : item.end] for item in message.mentions] == ["@alpha", "@beta"]
    assert [item.message_id for item in comms.bus.inbox("alpha")] == [message.message_id]
    assert [item.message_id for item in comms.bus.inbox("beta")] == [message.message_id]
    assert wire(tmp_path).views.channel_history("#team")[0].mentions == message.mentions
    assert Message.from_wire(message.to_wire()) == message
    scheduled = ScheduledTurn.incoming(message)
    assert scheduled.reply_target == "#team"
    assert "Response policy: mentioned_only" in scheduled.prompt
    assert "only resolved mentioned identities may respond: @alpha, @beta" in scheduled.prompt
    assert "unmentioned observers dismiss quietly" in scheduled.prompt
    assert "delivery and history remain #team" in scheduled.prompt


def test_aliases_resolve_but_emails_paths_and_unknown_names_are_plain_text(tmp_path):
    comms = wire(tmp_path)
    comms.threads.register(Thread("alpha", frozenset({"team"}), str(tmp_path)))
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
    comms = wire(tmp_path)
    for name in ("alpha", "beta"):
        comms.threads.register(Thread(name, frozenset({"team"}), str(tmp_path)))

    collective = comms.messaging.send_user_message(
        "#team", "@unknown can anyone answer?", worktree=str(tmp_path)
    )
    assert not collective.mentions
    assert collective.response_policy is CollectivePolicy.instance()
    assert collective.response_eligibility(("alpha", "beta")).recipients == (
        "alpha",
        "beta",
    )
    assert collective.starts_turn_for("alpha")
    assert collective.starts_turn_for("beta")
    collective_prompt = ScheduledTurn.incoming(collective).prompt
    assert "Response policy: collective; channel members may respond" in collective_prompt
    assert "delivery and history remain #team" in collective_prompt

    mentioned = comms.messaging.send_user_message("#team", "@beta please answer", worktree=str(tmp_path))
    eligibility = mentioned.response_eligibility(("alpha", "beta"))
    assert eligibility.policy is MentionedOnlyPolicy.instance()
    assert eligibility.recipients == ("beta",)
    assert not mentioned.starts_turn_for("alpha")
    assert mentioned.starts_turn_for("beta")
    assert mentioned.target == "#team"
    mentioned_prompt = ScheduledTurn.incoming(mentioned).prompt
    assert "only resolved mentioned identities may respond: @beta" in mentioned_prompt
    assert "unmentioned observers dismiss quietly" in mentioned_prompt

    agent_chatter = comms.messaging.send_message("alpha", "#team", "status only")
    assert agent_chatter.response_policy is InformationalPolicy.instance()
    assert not agent_chatter.response_eligibility(("beta",)).recipients
    assert not agent_chatter.starts_turn_for("beta")
    agent_prompt = ScheduledTurn.incoming(agent_chatter).prompt
    assert "Response policy: informational; observe and dismiss without replying" in agent_prompt
    assert "delivery and history remain #team" in agent_prompt

    informational = comms.messaging.send_message("alpha", "#team", "notice only", notice=True)
    assert informational.response_policy is InformationalPolicy.instance()
    assert not informational.response_eligibility(("alpha", "beta")).recipients
    assert not informational.starts_turn_for("beta")
    assert "Response policy: informational; observe and dismiss without replying" in (
        ScheduledTurn.incoming(informational).prompt
    )


@pytest.mark.parametrize(
    ("sender_role", "unmentioned_policy"),
    (
        (ThreadRole.USER, CollectivePolicy.instance()),
        (ThreadRole.AGENT, InformationalPolicy.instance()),
    ),
)
def test_unmentioned_channel_policy_uses_typed_sender_role(sender_role, unmentioned_policy):
    unmentioned = Message(
        "sender",
        "#team",
        "status for the team",
        MessageType.INFO,
        sender_role=sender_role,
    )
    assert unmentioned.response_policy is unmentioned_policy
    assert unmentioned.starts_turn is (unmentioned_policy is CollectivePolicy.instance())
    assert unmentioned.starts_turn_for("alpha") is (
        unmentioned_policy is CollectivePolicy.instance()
    )
    assert unmentioned.target == "#team"

    mentioned = Message(
        "sender",
        "#team",
        "@beta investigate",
        MessageType.INFO,
        sender_role=sender_role,
        mentions=(ThreadMention("beta", 0, 5),),
    )
    assert mentioned.response_policy is MentionedOnlyPolicy.instance()
    assert not mentioned.starts_turn
    assert not mentioned.starts_turn_for("alpha")
    assert mentioned.starts_turn_for("beta")
    assert mentioned.target == "#team"

    notice = Message(
        "sender",
        "#team",
        "delivery failed",
        MessageType.ALERT,
        sender_role=sender_role,
        notice=True,
    )
    assert notice.response_policy is InformationalPolicy.instance()
    assert not notice.starts_turn_for("alpha")


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
    from agent_comms import MessageType

    comms = wire(tmp_path)
    for name in ("sender", "owner"):
        comms.threads.register(Thread(name, frozenset({"team"}), str(tmp_path)))
    # A notice never wakes its recipient, so failure feedback cannot loop.
    comms.messaging.send("owner", "#team", "Delivery failed: usage limit", MessageType.ALERT, notice=True)
    notice = comms.views.channel_history("#team")[-1]
    assert notice.notice is True and not notice.starts_turn
    assert "sender" not in comms.views.last_sent_timestamps()
    assert Message.from_wire(notice.to_wire()) == notice


def test_model_tool_changes_own_and_another_thread(tmp_path, monkeypatch):
    from agent_comms import invoke_tool

    comms = wire(tmp_path)
    comms.threads.register(Thread("owner", frozenset(), str(tmp_path)))
    comms.threads.register(Thread("peer", frozenset(), str(tmp_path)))
    monkeypatch.setenv("PI_AGENT_ID", "owner")
    result = invoke_tool(comms, "comms_model", {"model": "openrouter/own"})
    assert result == {"thread": "owner", "model": "openrouter/own", "thinking_level": None}
    result = invoke_tool(
        comms,
        "comms_model",
        {"thread": "peer", "model": "openai-codex/gpt-5.6-sol", "thinking_level": "high"},
    )
    assert result["thread"] == "peer" and result["thinking_level"] == "high"
    assert wire(tmp_path).registry.require("peer").model == "openai-codex/gpt-5.6-sol"


def test_dismiss_reports_mentions_and_advances_only_own_cursor(tmp_path, monkeypatch):
    from agent_comms import invoke_tool

    comms = wire(tmp_path)
    for name in ("alpha", "beta"):
        comms.threads.register(Thread(name, frozenset({"team"}), str(tmp_path)))
    comms.messaging.send_user_message("#team", "@alpha please review", worktree=str(tmp_path))
    assert comms.bus.pending_count("alpha", "#team") == 1
    assert comms.bus.pending_count("beta", "#team") == 1
    monkeypatch.setenv("PI_AGENT_ID", "beta")
    result = invoke_tool(comms, "comms_dismiss", {"target": "#team"})
    assert result == {
        "thread": "beta",
        "target": "#team",
        "acknowledged": 1,
        "mentioned": ("alpha",),
        "dismissed": True,
    }
    # beta no longer sees the ping; alpha's delivery is untouched.
    assert comms.bus.pending_count("beta", "#team") == 0
    assert comms.bus.pending_count("alpha", "#team") == 1


def test_dismiss_requires_a_target(tmp_path, monkeypatch):
    import pytest

    from agent_comms import invoke_tool

    comms = wire(tmp_path)
    comms.threads.register(Thread("beta", frozenset({"team"}), str(tmp_path)))
    monkeypatch.setenv("PI_AGENT_ID", "beta")
    with pytest.raises(ValueError, match="channel target"):
        invoke_tool(comms, "comms_dismiss", {"target": ""})


def test_committed_channel_mention_follows_recipient_renames_without_expanding_audience(tmp_path):
    comms = wire(tmp_path)
    for name, tags in [("alpha", {"team"}), ("observer", {"team"}), ("outsider", set())]:
        comms.threads.register(Thread(name, frozenset(tags), str(tmp_path)))
    message = comms.messaging.send_user_message("#team", "@alpha please review", worktree=str(tmp_path))
    comms.registry.rename("alpha", "renamed")
    comms.registry.rename("renamed", "final")
    aliases = comms.registry.snapshot().aliases
    assert message in comms.bus.incoming_page("final", after=0).messages
    assert message.starts_turn_for("final", aliases=aliases)
    assert not message.starts_turn_for("observer", aliases=aliases)
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
