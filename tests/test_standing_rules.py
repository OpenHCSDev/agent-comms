"""Standing rules read current state: a blocked goal leads the board until it is active."""
import time
from dataclasses import replace

import pytest

from agent_comms.heartbeat_subjects import TeamHistory
from agent_comms.nominal.text_digest import TextDigest
from agent_comms.planning.goal_actions import SetGoalAction
from agent_comms.planning.goal_states import (
    ActiveGoal,
    BlockedGoal,
    UnrecordedBlockGoal,
)
from agent_comms.planning.order_words import DirectiveText
from agent_comms.projects.checkouts import Checkout
from agent_comms.registry.threads import NativeThread
from agent_comms.storage.recorded_time import RecordedEventTime
from agent_comms.task_attachments.team_pings import RulePing, TeamDecisions, TeamPings
from agent_comms.team.standing import GoalBlocked, RequestStalled
from agent_comms.team.standing_rules import DirectiveUnacted, StandingRule


@pytest.mark.parametrize("rule, urgent, owner_flagged", (
    (StandingRule, False, False),
    (GoalBlocked, True, False),
    (DirectiveUnacted, False, True),
    (RequestStalled, True, True),
))
def test_alarm_capabilities_keep_their_independent_meanings(rule, urgent, owner_flagged):
    assert rule.is_urgent() is urgent
    assert rule.is_owner_flagged() is owner_flagged


def test_plain_messages_need_no_order_labels():
    assert DirectiveText.refused("Yes, go ahead.") == ()


@pytest.mark.parametrize("state, reason", (
    (BlockedGoal("Provider needs authorization: " + "x" * 220),
     "Provider needs authorization: " + "x" * 220),
    (UnrecordedBlockGoal(), "reason unavailable"),
))
def test_blocked_goal_leads_the_board_and_clears_when_active(comms, tmp_path, state, reason):
    """No order is needed; the current state owns appearance and disappearance."""
    comms.registry.declare(NativeThread("paper", frozenset(), Checkout(tmp_path)))
    comms.goals.update_goal("paper", SetGoalAction(text="Finish the paper"))
    thread = comms.registry.require("paper")
    comms.registry.register(replace(thread, goal=replace(thread.goal.require(), state=state)),
                            comms.registry.status("paper"))
    rule, = GoalBlocked.violations(comms)
    assert rule.thread == thread.incarnation
    assert rule.reason == reason
    assert reason[:200] in rule.line()
    assert "x" * 201 not in rule.line()

    now = time.time()
    old = RulePing(RequestStalled, TextDigest.of("old housekeeping"),
                   text="IDLE housekeeping", since=RecordedEventTime(now - 600))
    previous = TeamDecisions(thread.incarnation, (old,), (old,), ())
    history = TeamHistory.read(comms, frozenset({thread.name}))
    delivery = TeamPings((), (rule,), (previous,), now=now, history=history).delivery(
        thread.incarnation, published=(previous,))
    update = delivery.packet
    assert update.handled[0].urgent
    current = comms.registry.require("paper")
    comms.registry.register(
        replace(current, goal=replace(current.goal.require(), state=ActiveGoal())),
        comms.registry.status("paper"),
    )
    assert GoalBlocked.violations(comms) == ()
    cleared = TeamPings((), (), (update,), now=now + 60, history=history).delivery(
        thread.incarnation, published=(update,))
    assert cleared.packet.handled == ()
