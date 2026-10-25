from agent_comms.repo import Checkout
import json
import time
from dataclasses import replace

import pytest

from agent_comms.cli_bridges.claude import BoardLineRow
from agent_comms.cli_bridges.shared import Binding
from agent_comms.commander.standing import GoalBlocked, RequestStalled
from agent_comms.goal_actions import SetGoalAction
from agent_comms.goal_states import ActiveGoal, BlockedGoal, UnrecordedBlockGoal
from agent_comms.task_attachments.commander_pings import CommanderDecisions, CommanderUpdate, RulePing
from agent_comms.recorded_time import RecordedEventTime
from agent_comms.text_digest import TextDigest
from agent_comms_hooks.board import BoardLine


from agent_comms.session_file import RegistrySessionLocation
from agent_comms.todos import (DirectiveText)
from agent_comms.threads import Thread



def lines(comms, member, commander="lead"):
    return [found.line() for found in member.violations(comms, commander)]


def working(comms, name, created):
    """A unit whose native session records a turn now."""
    from datetime import UTC, datetime
    stamp = datetime.now(UTC).isoformat()
    path = comms.root / f"{name}-session.jsonl"
    rows = [dict(type="session", id=f"{name}-session", version=3, timestamp=stamp),
            dict(type="message", id=f"{name}-input", timestamp=stamp,
                 message=dict(role="user", content="Repair the owner",
                              timestamp=time.time() * 1000))]
    path.write_text("".join(json.dumps(row) + "\n" for row in rows))
    path.chmod(0o600)
    if name in comms.registry.all_threads():
        comms.registry.register(replace(comms.registry.require(name), session_file=RegistrySessionLocation.from_path(str(path), comms.root)))
    else:
        comms.registry.declare(Thread(
            name, frozenset(), Checkout.from_wire(str(comms.root)), session_file=RegistrySessionLocation.from_path(str(path), tmp_path), created_at=created))


def test_plain_messages_need_no_order_labels():
    assert DirectiveText.refused("Yes, go ahead.") == ()


@pytest.mark.parametrize("state, reason", (
    (BlockedGoal("Provider needs authorization: " + "x" * 220),
     "Provider needs authorization: " + "x" * 220),
    (UnrecordedBlockGoal(), "no reason recorded"),
))
def test_blocked_goal_leads_the_board_and_clears_when_active(comms, tmp_path, state, reason):
    """No order is needed; the current state owns appearance and disappearance."""
    comms.registry.declare(Thread("paper", frozenset(), Checkout.from_wire(str(tmp_path))))
    comms.goals.update_goal("paper", SetGoalAction(text="Finish the paper"))
    thread = comms.registry.require("paper")
    comms.registry.register(replace(thread, goal=replace(thread.goal.require(), state=state)),
                            comms.registry.status("paper"))
    rule, = GoalBlocked.violations(comms, "paper")
    assert rule.thread == thread.incarnation
    assert rule.reason == reason
    assert reason[:200] in rule.line()
    assert "x" * 201 not in rule.line()
    assert "unblock it or tell Tristan, this wake" in rule.line()

    now = time.time()
    old = RulePing(RequestStalled, TextDigest.of("old housekeeping"),
                   text="IDLE housekeeping", since=RecordedEventTime(now - 600))
    previous = CommanderDecisions(thread.incarnation, (old,), (old,), ())
    update = CommanderUpdate.derive(thread.incarnation, (), (rule,), previous, now=now)
    assert update is not None
    assert update.handled[0].urgent
    assert "seen for 0 min" in update.handled[0].render(now)
    urgent, = update.handled
    with Binding.open(tmp_path / "bridge") as db:
        BoardLineRow.record(db, CommanderDecisions(thread.incarnation, (old, urgent), (), ()))
        first, second = BoardLine.recorded(tmp_path / "bridge" / "delivery.sqlite3")
        assert first.urgent and first.text == rule.line()
        assert not second.urgent and second.text == old.text

        current = comms.registry.require("paper")
        comms.registry.register(
            replace(current, goal=replace(current.goal.require(), state=ActiveGoal())),
            comms.registry.status("paper"),
        )
        assert GoalBlocked.violations(comms, "paper") == ()
        cleared = CommanderUpdate.derive(thread.incarnation, (), (), update, now=now + 60)
        assert cleared is not None
        BoardLineRow.record(db, cleared)
        assert BoardLine.recorded(tmp_path / "bridge" / "delivery.sqlite3") == ()


